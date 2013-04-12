#!/usr/bin/env python
#
# LSST Data Management System
# Copyright 2008-2013 LSST Corporation.
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#

import lsst.afw.table
from lsst.daf.base import PropertyList
from lsst.meas.algorithms import SourceMeasurementTask
from lsst.pex.config import Config, ConfigurableField, DictField, Field, FieldValidationError
from lsst.pipe.base import Task, CmdLineTask, Struct, timeMethod
from .references import CoaddSrcReferencesTask

__all__ = ("ForcedPhotImageTask",)

class ForcedPhotImageConfig(Config):
    """Configuration for forced photometry.
    """
    references = ConfigurableField(target=CoaddSrcReferencesTask, doc="Retrieve reference source catalog")
    measurement = ConfigurableField(target=SourceMeasurementTask, doc="measurement subtask")
    copyColumns = DictField(
        keytype=str, itemtype=str, doc="Mapping of reference columns to source columns",
        default={"id": "objectId", "parent":"parentObjectId"}
        )

    def _getTweakCentroids(self):
        return self.measurement.centroider.name is not None

    def _setTweakCentroids(self, doTweak):
        if doTweak:
            self.measurement.centroider.name = "centroid.sdss"
            self.measurement.algorithms.names -= ["centroid.sdss"]
            self.measurement.algorithms.names |= ["skycoord", "centroid.record"]
            self.measurement.slots.centroid = "centroid.sdss"
        else:
            self.measurement.centroider.name = None
            self.measurement.algorithms.names |= ["centroid.sdss", "centroid.record"]
            self.measurement.algorithms.names -= ["skycoord"]
            self.measurement.slots.centroid = "centroid.record"

    doTweakCentroids = property(
        _getTweakCentroids, _setTweakCentroids,
        doc=("A meta-config option (just a property, really) that sets whether to tweak centroids during "
             "measurement by modifying several other config options")
    )

    def setDefaults(self):
        self.doTweakCentroids = False
        self.measurement.doReplaceWithNoise = False

    def validate(self):
        if self.measurement.doReplaceWithNoise:
            raise FieldValidationError(
                field=SourceMeasurementTask.Configclass.doReplaceWithNoise,
                config=self,
                msg="doReplaceWithNoise is not valid for forced photometry"
                )

class ForcedPhotImageTask(CmdLineTask):
    """Base class for performing forced measurement, in which the results (often just centroids) from
    regular measurement on another image are used to perform restricted measurement on a new image.

    This task is not directly usable as a CmdLineTask; subclasses must:
     - Set the _DefaultName class attribute
     - Implement makeIdFactory
     - Implement fetchReferences
    """

    ConfigClass = ForcedPhotImageConfig
    dataPrefix = ""  # Name to prepend to all input and output datasets (e.g. 'goodSeeingCoadd_')

    def __init__(self, *args, **kwargs):
        super(ForcedPhotImageTask, self).__init__(*args, **kwargs)
        # this schema will contain all the outputs from the forced measurement, but *not*
        # the fields copied from the reference catalog (see comment below)
        self.measSchema = lsst.afw.table.SourceTable.makeMinimalSchema()
        self.algMetadata = PropertyList()
        self.makeSubtask("measurement", schema=self.measSchema, algMetadata=self.algMetadata, isForced=True)
        self.makeSubtask("references")
        self.schema = None

    # The implementation for defining the schema for the forced photometry catalog is perhaps not quite
    # ideal; we'd like to be able to generate the schema in the constructor, but because it depends on
    # the reference schema (which we can't generally get without a butler), we can't do it there.  But
    # we can't wait until we process the first dataRef, because getSchemaCatalogs() needs to be called
    # before then by Task.writeSchemas.  The solution I have used here is to add a butler argument to
    # getSchemaCatalogs() (which requires changes in pipe_base).  We then build the schema upon first
    # request.  An alternative would be to pass a butler argument to task constructors, which would
    # have required slightly more disruptive changes in pipe_base, but may provide a cleaner solution.
    def _buildSchema(self, butler):
        # we make a SchemaMapper to transfer fields from the reference catalog
        refSchema = self.references.getSchema(butler)
        self.schemaMapper = lsst.afw.table.SchemaMapper(refSchema)
        # but we add the schema with the forced measurement fields first, before any mapped fields
        # (just because that's easier with the SchemaMapper API)
        self.schemaMapper.addMinimalSchema(self.measSchema, False)
        for refName, targetName in self.config.copyColumns.items():
            refItem = refSchema.find(refName)
            self.schemaMapper.addMapping(refItem.key, targetName)
        self.schema = self.schemaMapper.getOutputSchema()

    def getSchemaCatalogs(self, butler):
        if self.schema is None:
            self._buildSchema(butler)
        catalog = lsst.afw.table.SourceCatalog(self.schema)
        return {self.dataPrefix + "forced_src": catalog}

    def makeIdFactory(self, dataRef):
        """Hook for derived classes to define how to make an IdFactory for forced sources.

        Note that this is for forced source IDs, not object IDs, which are usually handled by
        the copyColumns config option.
        """
        raise NotImplementedError()

    def fetchReferences(self, dataRef, exposure):
        """Hook for derived classes to define how to get references objects.

        Derived classes should call one of the fetch* methods on the references subtask,
        but which one they call depends on whether the region to get references for is a
        easy to describe in patches (as it would be when doing forced measurements on a
        coadd), or is just an arbitrary box (as it would be for CCD forced measurements).
        """
        raise NotImplementedError()

    def getExposure(self, dataRef):
        """Read input exposure on which to perform the measurements

        @param dataRef       Data reference from butler
        """
        return dataRef.get(self.dataPrefix + "calexp", immediate=True)

    def writeOutput(self, dataRef, sources):
        """Write forced source table

        @param dataRef  Data reference from butler
        @param sources  SourceCatalog to save
        """
        dataRef.put(sources, self.dataPrefix + "forced_src")

    def generateSources(self, dataRef, references):
        """Generate sources to be measured, copying any fields in self.config.copyColumns

        @param dataRef     Data reference from butler
        @param references  Sequence (not necessarily a SourceCatalog) of reference sources
        @param idFactory   Factory to generate unique ids for forced sources
        @return Source catalog ready for measurement
        """
        if self.schema is None:
            self._buildSchema(dataRef.butlerSubset.butler)
        idFactory = self.makeIdFactory(dataRef)
        table = lsst.afw.table.SourceTable.make(self.schema, idFactory)
        sources = lsst.afw.table.SourceCatalog(table)
        table = sources.table
        table.setMetadata(self.algMetadata)
        table.preallocate(len(references))
        for ref in references:
            sources.addNew().assign(ref, self.schemaMapper)
        return sources

    @lsst.pipe.base.timeMethod
    def run(self, dataRef):
        """Perform forced measurement on the exposure defined by the given dataref.

        The dataRef must contain a 'tract' key, which is used to resolve the correct references
        in the presence of tract overlaps, and also defines the WCS of the reference sources.
        """
        refWcs = self.references.getWcs(dataRef)
        exposure = self.getExposure(dataRef)
        references = list(self.fetchReferences(dataRef, exposure))
        self.log.info("Performing forced measurement on %d sources" % len(references))
        sources = self.generateSources(dataRef, references)
        self.measurement.run(exposure, sources, references=references, refWcs=refWcs)
        self.writeOutput(dataRef, sources)
        return Struct(sources=sources)
