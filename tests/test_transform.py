#
# LSST Data Management System
# Copyright 2008-2015 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#
"""
Test the basic operation of measurement transformations.

We test measurement transforms in two ways:

First, we construct and run a simple TransformTask on the (mocked) results of
measurement tasks. The same test is carried out against both
SingleFrameMeasurementTask and ForcedMeasurementTask, on the basis that the
transformation system should be agnostic as to the origin of the source
catalog it is transforming.

Secondly, we use data from the obs_test package to demonstrate that the
transformtion system and its interface package are capable of operating on
data processed by the rest of the stack.

For the purposes of testing, we define a "TrivialMeasurement" plugin and
associated transformation. Rather than building a catalog by measuring
genuine SourceRecords, we directly populate a catalog following the
TrivialMeasurement schema, then check that it is transformed properly by the
TrivialMeasurementTransform.
"""
import contextlib
import os
import shutil
import tempfile
import unittest

import lsst.utils
import lsst.afw.table as afwTable
import lsst.afw.geom as afwGeom
import lsst.daf.persistence as dafPersist
import lsst.meas.base as measBase
import lsst.utils.tests
from lsst.pipe.tasks.multiBand import MeasureMergedCoaddSourcesConfig
from lsst.pipe.tasks.processCcd import ProcessCcdTask, ProcessCcdConfig
from lsst.pipe.tasks.transformMeasurement import (TransformConfig, TransformTask, SrcTransformTask,
                                                  RunTransformConfig, CoaddSrcTransformTask)

PLUGIN_NAME = "base_TrivialMeasurement"

# Rather than providing real WCS and calibration objects to the
# transformation, we use this simple placeholder to keep track of the number
# of times it is accessed.


class Placeholder:

    def __init__(self):
        self.count = 0

    def increment(self):
        self.count += 1


class TrivialMeasurementTransform(measBase.transforms.MeasurementTransform):

    def __init__(self, config, name, mapper):
        """Pass through all input fields to the output, and add a new field
        named after the measurement with the suffix "_transform".
        """
        measBase.transforms.MeasurementTransform.__init__(self, config, name, mapper)
        for key, field in mapper.getInputSchema().extract(name + "*").values():
            mapper.addMapping(key)
        self.key = mapper.editOutputSchema().addField(name + "_transform", type="D", doc="transformed dummy")

    def __call__(self, inputCatalog, outputCatalog, wcs, calib):
        """Transform inputCatalog to outputCatalog.

        We update the wcs and calib placeholders to indicate that they have
        been seen in the transformation, but do not use their values.

        @param[in]  inputCatalog  SourceCatalog of measurements for transformation.
        @param[out] outputCatalog BaseCatalog of transformed measurements.
        @param[in]  wcs           Dummy WCS information; an instance of Placeholder.
        @param[in]  calib         Dummy calibration information; an instance of Placeholder.
        """
        if hasattr(wcs, "increment"):
            wcs.increment()
        if hasattr(calib, "increment"):
            calib.increment()
        inColumns = inputCatalog.getColumnView()
        outColumns = outputCatalog.getColumnView()
        outColumns[self.key] = -1.0 * inColumns[self.name]


class TrivialMeasurementBase:

    """Default values for a trivial measurement plugin, subclassed below"""
    @staticmethod
    def getExecutionOrder():
        return 0

    @staticmethod
    def getTransformClass():
        return TrivialMeasurementTransform

    def measure(self, measRecord, exposure):
        measRecord.set(self.key, 1.0)


@measBase.register(PLUGIN_NAME)
class SFTrivialMeasurement(TrivialMeasurementBase, measBase.sfm.SingleFramePlugin):

    """Single frame version of the trivial measurement"""

    def __init__(self, config, name, schema, metadata):
        measBase.sfm.SingleFramePlugin.__init__(self, config, name, schema, metadata)
        self.key = schema.addField(name, type="D", doc="dummy field")


@measBase.register(PLUGIN_NAME)
class ForcedTrivialMeasurement(TrivialMeasurementBase, measBase.forcedMeasurement.ForcedPlugin):

    """Forced frame version of the trivial measurement"""

    def __init__(self, config, name, schemaMapper, metadata):
        measBase.forcedMeasurement.ForcedPlugin.__init__(self, config, name, schemaMapper, metadata)
        self.key = schemaMapper.editOutputSchema().addField(name, type="D", doc="dummy field")


class TransformTestCase(lsst.utils.tests.TestCase):

    def _transformAndCheck(self, measConf, schema, transformTask):
        """Check the results of applying transformTask to a SourceCatalog.

        @param[in] measConf       Measurement plugin configuration.
        @param[in] schema         Input catalog schema.
        @param[in] transformTask  Instance of TransformTask to be applied.

        For internal use by this test case.
        """
        # There should now be one transformation registered per measurement plugin.
        self.assertEqual(len(measConf.plugins.names), len(transformTask.transforms))

        # Rather than do a real measurement, we use a dummy source catalog
        # containing a source at an arbitrary position.
        inCat = afwTable.SourceCatalog(schema)
        r = inCat.addNew()
        r.setCoord(afwGeom.SpherePoint(0.0, 11.19, afwGeom.degrees))
        r[PLUGIN_NAME] = 1.0

        wcs, calib = Placeholder(), Placeholder()
        outCat = transformTask.run(inCat, wcs, calib)

        # Check that all sources have been transformed appropriately.
        for inSrc, outSrc in zip(inCat, outCat):
            self.assertEqual(outSrc[PLUGIN_NAME], inSrc[PLUGIN_NAME])
            self.assertEqual(outSrc[PLUGIN_NAME + "_transform"], inSrc[PLUGIN_NAME] * -1.0)
            for field in transformTask.config.toDict()['copyFields']:
                self.assertEqual(outSrc.get(field), inSrc.get(field))

        # Check that the wcs and calib objects were accessed once per transform.
        self.assertEqual(wcs.count, len(transformTask.transforms))
        self.assertEqual(calib.count, len(transformTask.transforms))

    def testSingleFrameMeasurementTransform(self):
        """Test applying a transform task to the results of single frame measurement."""
        schema = afwTable.SourceTable.makeMinimalSchema()
        sfmConfig = measBase.SingleFrameMeasurementConfig(plugins=[PLUGIN_NAME])
        # We don't use slots in this test
        for key in sfmConfig.slots:
            setattr(sfmConfig.slots, key, None)
        sfmTask = measBase.SingleFrameMeasurementTask(schema, config=sfmConfig)
        transformTask = TransformTask(measConfig=sfmConfig,
                                      inputSchema=sfmTask.schema, outputDataset="src")
        self._transformAndCheck(sfmConfig, sfmTask.schema, transformTask)

    def testForcedMeasurementTransform(self):
        """Test applying a transform task to the results of forced measurement."""
        schema = afwTable.SourceTable.makeMinimalSchema()
        forcedConfig = measBase.ForcedMeasurementConfig(plugins=[PLUGIN_NAME])
        # We don't use slots in this test
        for key in forcedConfig.slots:
            setattr(forcedConfig.slots, key, None)
        forcedConfig.copyColumns = {"id": "objectId", "parent": "parentObjectId"}
        forcedTask = measBase.ForcedMeasurementTask(schema, config=forcedConfig)
        transformConfig = TransformConfig(copyFields=("objectId", "coord_ra", "coord_dec"))
        transformTask = TransformTask(measConfig=forcedConfig,
                                      inputSchema=forcedTask.schema, outputDataset="forced_src",
                                      config=transformConfig)
        self._transformAndCheck(forcedConfig, forcedTask.schema, transformTask)


@contextlib.contextmanager
def tempDirectory(*args, **kwargs):
    """A context manager which provides a temporary directory and automatically cleans up when done."""
    dirname = tempfile.mkdtemp(*args, **kwargs)
    try:
        yield dirname
    finally:
        shutil.rmtree(dirname, ignore_errors=True)


class RunTransformTestCase(lsst.utils.tests.TestCase):

    def testInterface(self):
        obsTestDir = lsst.utils.getPackageDir('obs_test')
        inputDir = os.path.join(obsTestDir, "data", "input")

        # Configure a ProcessCcd task such that it will return a minimal
        # number of measurements plus our test plugin.
        cfg = ProcessCcdConfig()
        cfg.calibrate.measurement.plugins.names = ["base_SdssCentroid", "base_SkyCoord", PLUGIN_NAME]
        cfg.calibrate.measurement.slots.shape = None
        cfg.calibrate.measurement.slots.psfFlux = None
        cfg.calibrate.measurement.slots.apFlux = None
        cfg.calibrate.measurement.slots.instFlux = None
        cfg.calibrate.measurement.slots.modelFlux = None
        cfg.calibrate.measurement.slots.calibFlux = None
        # no reference catalog, so...
        cfg.calibrate.doAstrometry = False
        cfg.calibrate.doPhotoCal = False
        # disable aperture correction because we aren't measuring aperture flux
        cfg.calibrate.doApCorr = False
        # Extendedness requires modelFlux, disabled above.
        cfg.calibrate.catalogCalculation.plugins.names.discard("base_ClassificationExtendedness")

        # Process the test data with ProcessCcd then perform a transform.
        with tempDirectory() as tempDir:
            measResult = ProcessCcdTask.parseAndRun(args=[inputDir, "--output", tempDir, "--id", "visit=1"],
                                                    config=cfg, doReturnResults=True)
            trArgs = [tempDir, "--output", tempDir, "--id", "visit=1",
                      "-c", "inputConfigType=processCcd_config"]
            trResult = SrcTransformTask.parseAndRun(args=trArgs, doReturnResults=True)

            # It should be possible to reprocess the data through a new transform task with exactly
            # the same configuration without throwing. This check is useful since we are
            # constructing the task on the fly, which could conceivably cause problems with
            # configuration/metadata persistence.
            trResult = SrcTransformTask.parseAndRun(args=trArgs, doReturnResults=True)

        measSrcs = measResult.resultList[0].result.calibRes.sourceCat
        trSrcs = trResult.resultList[0].result

        # The length of the measured and transformed catalogs should be the same.
        self.assertEqual(len(measSrcs), len(trSrcs))

        # Each source should have been measured & transformed appropriately.
        for measSrc, trSrc in zip(measSrcs, trSrcs):
            # The TrivialMeasurement should be transformed as defined above.
            self.assertEqual(trSrc[PLUGIN_NAME], measSrc[PLUGIN_NAME])
            self.assertEqual(trSrc[PLUGIN_NAME + "_transform"], -1.0 * measSrc[PLUGIN_NAME])

            # The SdssCentroid should be transformed to celestial coordinates.
            # Checking that the full transformation has been done correctly is
            # out of scope for this test case; we just ensure that there's
            # plausible position in the transformed record.
            trCoord = afwTable.CoordKey(trSrcs.schema["base_SdssCentroid"]).get(trSrc)
            self.assertAlmostEqual(measSrc.getCoord().getLongitude(), trCoord.getLongitude())
            self.assertAlmostEqual(measSrc.getCoord().getLatitude(), trCoord.getLatitude())


class CoaddTransformTestCase(lsst.utils.tests.TestCase):
    """Check that CoaddSrcTransformTask is set up properly.

    RunTransformTestCase, above, has tested the basic RunTransformTask mechanism.
    Here, we just check that it is appropriately adapted for coadds.
    """
    MEASUREMENT_CONFIG_DATASET = "measureCoaddSources_config"

    # The following are hard-coded in lsst.pipe.tasks.multiBand:
    SCHEMA_SUFFIX = "Coadd_meas_schema"
    SOURCE_SUFFIX = "Coadd_meas"
    CALEXP_SUFFIX = "Coadd_calexp"

    def setUp(self):
        # We need a temporary repository in which we can store test configs.
        self.repo = tempfile.mkdtemp()
        with open(os.path.join(self.repo, "_mapper"), "w") as f:
            f.write("lsst.obs.test.TestMapper")
        self.butler = dafPersist.Butler(self.repo)

        # Persist a coadd measurement config.
        # We disable all measurement plugins so that there's no actual work
        # for the TransformTask to do.
        measCfg = MeasureMergedCoaddSourcesConfig()
        measCfg.measurement.plugins.names = []
        self.butler.put(measCfg, self.MEASUREMENT_CONFIG_DATASET)

        # Record the type of coadd on which our supposed measurements have
        # been carried out: we need to check this was propagated to the
        # transformation task.
        self.coaddName = measCfg.coaddName

        # Since we disabled all measurement plugins, our catalog can be
        # simple.
        c = afwTable.SourceCatalog(afwTable.SourceTable.makeMinimalSchema())
        self.butler.put(c, self.coaddName + self.SCHEMA_SUFFIX)

        # Our transformation config needs to know the type of the measurement
        # configuration.
        trCfg = RunTransformConfig()
        trCfg.inputConfigType = self.MEASUREMENT_CONFIG_DATASET

        self.transformTask = CoaddSrcTransformTask(config=trCfg, log=None, butler=self.butler)

    def tearDown(self):
        del self.butler
        del self.transformTask
        shutil.rmtree(self.repo)

    def testCoaddName(self):
        """Check that we have correctly derived the coadd name."""
        self.assertEqual(self.transformTask.coaddName, self.coaddName)

    def testSourceType(self):
        """Check that we have correctly derived the type of the measured sources."""
        self.assertEqual(self.transformTask.sourceType, self.coaddName + self.SOURCE_SUFFIX)

    def testCalexpType(self):
        """Check that we have correctly derived the type of the measurement images."""
        self.assertEqual(self.transformTask.calexpType, self.coaddName + self.CALEXP_SUFFIX)


class MyMemoryTestCase(lsst.utils.tests.MemoryTestCase):
    pass


def setup_module(module):
    lsst.utils.tests.init()


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()
