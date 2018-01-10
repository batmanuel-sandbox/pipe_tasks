from __future__ import absolute_import, division, print_function

#
# LSST Data Management System
# Copyright 2008-2016 AURA/LSST.
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
# see <https://www.lsstcorp.org/LegalNotices/>.
#

from collections import namedtuple
import numpy
from scipy.ndimage.interpolation import shift as scipyShift
from lsst.afw.coor.refraction import differentialRefraction
import lsst.afw.geom as afwGeom
import lsst.afw.image as afwImage
import lsst.afw.image.utils as afwImageUtils
import lsst.afw.math as afwMath
import lsst.coadd.utils as coaddUtils
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
from lsst.pipe.tasks.assembleCoadd import _subBBoxIter
from lsst.pipe.tasks.assembleCoadd import AssembleCoaddTask
# from lsst.pipe.tasks.assembleCoadd import AssembleCoaddConfig
from lsst.pipe.tasks.assembleCoadd import CompareWarpAssembleCoaddTask
from lsst.pipe.tasks.assembleCoadd import CompareWarpAssembleCoaddConfig

__all__ = ["DcrAssembleCoaddTask"]


class DcrAssembleCoaddConfig(CompareWarpAssembleCoaddConfig):
    filterName = pexConfig.Field(
        dtype=str,
        doc="Common name of the band-defining filter of th observations.",
        default='g',
    )
    lambdaEff = pexConfig.Field(
        dtype=float,
        doc="Effective wavelength of the filter, in nm.",
        default=afwImageUtils.Filter(filterName).getFilterProperty().getLambdaEff(),
    )
    filterWidth = pexConfig.Field(
        dtype=float,
        doc="FWHM of the filter transmission curve, in nm.",
        default=lambdaEff*0.2,
    )
    bufferSize = pexConfig.Field(
        dtype=int,
        doc="Number of pixels to grow the subregion bounding box by.",
        default=5,
    )
    useFFT = pexConfig.Field(
        dtype=bool,
        doc="Option to use use Fourier transforms for the convolutions.",
        default=False,
    )
    usePsf = pexConfig.Field(
        dtype=bool,
        doc="Option to use the PSF as part of the convolution; requires `useFFT=True`.",
        default=False,
    )
    dcrNSubbands = pexConfig.Field(
        dtype=int,
        doc="Number of sub-bands to forward model chromatic effects to fit the supplied exposures.",
        default=3,
    )
    maxNIter = pexConfig.Field(
        dtype=int,
        doc="Maximum number of iterations of forward modeling.",
        default=8,
    )
    convergenceThreshold = pexConfig.Field(
        dtype=float,
        doc="Target change in convergence between iteration of forward modeling.",
        default=0.001,
    )
    assembleStaticSkyModel = pexConfig.ConfigurableField(
        target=AssembleCoaddTask,
        doc="Task to assemble an artifact-free, PSF-matched Coadd to serve as a"
            " naive/first-iteration model of the static sky.",
    )

    def setDefaults(self):
        CompareWarpAssembleCoaddConfig.setDefaults(self)
        if self.usePsf:
            self.useFFT = True


class DcrAssembleCoaddTask(CompareWarpAssembleCoaddTask):

    ConfigClass = DcrAssembleCoaddConfig
    _DefaultName = "dcrAssembleCoadd"

    def __init__(self, *args, **kwargs):
        """!
        \brief Initialize the task and make the \ref AssembleCoadd_ "assembleStaticSkyModel" subtask.
        """
        CompareWarpAssembleCoaddTask.__init__(self, *args, **kwargs)

    @pipeBase.timeMethod
    def run(self, dataRef, selectDataList=[]):
        """!
        \brief Assemble a coadd from a set of Warps

        Coadd a set of Warps. Compute weights to be applied to each Warp and find scalings to
        match the photometric zeropoint to a reference Warp. Optionally, match backgrounds across
        Warps if the background has not already been removed. Assemble the Warps using
        \ref assemble. Interpolate over NaNs and optionally write the coadd to disk. Return the coadded
        exposure.

        \anchor runParams
        \param[in] dataRef: Data reference defining the patch for coaddition and the reference Warp
                        (if config.autoReference=False). Used to access the following data products:
                        - [in] self.config.coaddName + "Coadd_skyMap"
                        - [in] self.config.coaddName + "Coadd_ + <warpType> + "Warp" (optionally)
                        - [out] self.config.coaddName + "Coadd"
        \param[in] selectDataList[in]: List of data references to Warps. Data to be coadded will be
                                   selected from this list based on overlap with the patch defined by dataRef.

        \return a pipeBase.Struct with fields:
                 - coaddExposure: coadded exposure
                 - nImage: exposure count image
        """
        skyInfo = self.getSkyInfo(dataRef)
        calExpRefList = self.selectExposures(dataRef, skyInfo, selectDataList=selectDataList)
        if len(calExpRefList) == 0:
            self.log.warn("No exposures to coadd")
            return
        self.log.info("Coadding %d exposures", len(calExpRefList))

        tempExpRefList = self.getTempExpRefList(dataRef, calExpRefList)
        inputData = self.prepareInputs(tempExpRefList)
        self.log.info("Found %d %s", len(inputData.tempExpRefList),
                      self.getTempExpDatasetName(self.warpType))
        if len(inputData.tempExpRefList) == 0:
            self.log.warn("No coadd temporary exposures found")
            return

        supplementaryData = self.makeSupplementaryData(dataRef, selectDataList)
        # nImage is created if it is requested by self.config.  Otherwise, None.

        retStructGen = self.assemble(
            skyInfo, inputData.tempExpRefList, inputData.imageScalerList, inputData.weightList,
            supplementaryData=supplementaryData,
        )
        coaddExp = None
        for retStruct in retStructGen:

            if self.config.doInterp:
                self.interpImage.run(retStruct.coaddExposure.getMaskedImage(), planeName="NO_DATA")
                # The variance must be positive; work around for DM-3201.
                varArray = retStruct.coaddExposure.getMaskedImage().getVariance().getArray()
                varArray[:] = numpy.where(varArray > 0, varArray, numpy.inf)

            if self.config.doMaskBrightObjects:
                brightObjectMasks = self.readBrightObjectMasks(dataRef)
                self.setBrightObjectMasks(retStruct.coaddExposure, dataRef.dataId, brightObjectMasks)

            if self.config.doWrite:
                self.log.info("Persisting dcrCoadd")
                dataRef.put(retStruct.coaddExposure, "dcrCoadd", subfilter=retStruct.subFilter)

            if coaddExp is None:
                coaddExp = retStruct.coaddExposure
            else:
                mimage = coaddExp.getMaskedImage()
                mimage += retStruct.coaddExposure.getMaskedImage()
        if self.config.doWrite:
            self.log.info("Persisting %s" % self.getCoaddDatasetName(self.warpType))
            dataRef.put(retStruct.coaddExposure, self.getCoaddDatasetName(self.warpType))
            if retStruct.nImage is not None:
                dataRef.put(retStruct.nImage, self.getCoaddDatasetName(self.warpType)+'_nImage')
        return pipeBase.Struct(coaddExposure=coaddExp, nImage=retStruct.nImage)

    def assemble(self, skyInfo, tempExpRefList, imageScalerList, weightList,
                 altMaskList=None, supplementaryData=None, *args, **kwargs):
        """!
        \brief Assemble the coadd

        Requires additional inputs Struct `supplementaryData` to contain a `templateCoadd` that serves
        as the model of the static sky.

        Find artifacts and apply them to the warps' masks creating a list of alternative masks with a
        new "CLIPPED" plane and updated "NO_DATA" plane.
        Then pass these alternative masks to the base class's assemble method.

        @param skyInfo: Patch geometry information
        @param tempExpRefList: List of data references to warps
        @param imageScalerList: List of image scalers
        @param weightList: List of weights
        @param supplementaryData: PipeBase.Struct containing a templateCoadd

        return coadd exposure
        """
        # templateCoadd = supplementaryData.templateCoadd
        # spanSetMaskList = self.findArtifacts(templateCoadd, tempExpRefList, imageScalerList)
        # maskList = self.computeAltMaskList(tempExpRefList, spanSetMaskList)
        # badMaskPlanes = self.config.badMaskPlanes[:]
        # badMaskPlanes.append("CLIPPED")
        # badPixelMask = afwImage.Mask.getPlaneBitMask(badMaskPlanes)

        # templateCoadd = supplementaryData.coaddExposure
        # spanSetMaskList = CompareWarpAssembleCoaddTask.findArtifacts(self, templateCoadd,
        #                                                              tempExpRefList, imageScalerList)
        # maskList = CompareWarpAssembleCoaddTask.computeAltMaskList(self, tempExpRefList, spanSetMaskList)
        badMaskPlanes = self.config.badMaskPlanes[:]
        badMaskPlanes.append("CLIPPED")
        badPixelMask = afwImage.Mask.getPlaneBitMask(badMaskPlanes)
        subBandImages = self.dcrDivideCoadd(supplementaryData.templateCoadd)

        statsCtrl, statsFlags = self.prepareStats(skyInfo, mask=badPixelMask)

        subregionSizeArr = self.config.subregionSize
        subregionSize = afwGeom.Extent2I(subregionSizeArr[0], subregionSizeArr[1])

        if altMaskList is None:
            altMaskList = [None]*len(tempExpRefList)
        for subBBox in _subBBoxIter(skyInfo.bbox, subregionSize):
            iter = 0
            convergenceMetric = self.calculateConvergence(subBandImages, subBBox, tempExpRefList,
                                                          imageScalerList, weightList, altMaskList)
            convergenceList = [convergenceMetric]
            convergenceCheck = convergenceMetric
            data_check = [numpy.std(model[subBBox].getImage().getArray()) for model in subBandImages]
            self.log.info("Deviation of model in coadd %s: %s", subBBox, data_check)
            while convergenceCheck > self.config.convergenceThreshold:
                self.log.info("Iteration %s with convergence %s", iter, convergenceMetric)
                try:
                    self.dcrAssembleSubregion(subBandImages, subBBox, tempExpRefList, imageScalerList,
                                              weightList, altMaskList, statsFlags, statsCtrl,
                                              convergenceMetric)
                    convergenceMetric = self.calculateConvergence(subBandImages, subBBox, tempExpRefList,
                                                                  imageScalerList, weightList, altMaskList)
                    convergenceCheck = convergenceList[-1] - convergenceMetric
                    convergenceList.append(convergenceMetric)
                except Exception as e:
                    self.log.warn("Error during iteration %s while computing coadd %s: %s", iter, subBBox, e)
                    break
                if iter > self.config.maxNIter:
                    break
                iter += 1
        dcrCoadd = self.fillCoadd(subBandImages, skyInfo, tempExpRefList, weightList)
        for subFilter, coadd in enumerate(dcrCoadd):
            yield pipeBase.Struct(coaddExposure=coadd, nImage=None, subFilter=subFilter)

    def prepareStats(self, skyInfo, mask=None):
        if mask is None:
            mask = self.getBadPixelMask()

        statsCtrl = afwMath.StatisticsControl()
        statsCtrl.setNumSigmaClip(self.config.sigmaClip)
        statsCtrl.setNumIter(self.config.clipIter)
        statsCtrl.setAndMask(mask)
        statsCtrl.setNanSafe(True)
        statsCtrl.setWeighted(True)
        statsCtrl.setCalcErrorFromInputVariance(True)
        for plane, threshold in self.config.maskPropagationThresholds.items():
            bit = afwImage.Mask.getMaskPlane(plane)
            statsCtrl.setMaskPropagationThreshold(bit, threshold)

        statsFlags = afwMath.stringToStatisticsProperty(self.config.statistic)

        return (statsCtrl, statsFlags)

    def dcrAssembleSubregion(self, dcrModel, bbox, tempExpRefList, imageScalerList, weightList,
                             altMaskList, statsFlags, statsCtrl, convergenceMetric):
        """!
        \brief Assemble the DCR coadd for a sub-region, .

        For each coaddTempExp, check for (and swap in) an alternative mask if one is passed. If background
        matching is enabled, add the background and background variance from each coaddTempExp. Remove mask
        planes listed in config.removeMaskPlanes, Finally, stack the actual exposures using
        \ref afwMath.statisticsStack "statisticsStack" with the statistic specified
        by statsFlags. Typically, the statsFlag will be one of afwMath.MEAN for a mean-stack or
        afwMath.MEANCLIP for outlier rejection using an N-sigma clipped mean where N and iterations
        are specified by statsCtrl.  Assign the stacked subregion back to the coadd.

        \param[in] coaddExposure: The target image for the coadd
        \param[in] bbox: Sub-region to coadd
        \param[in] tempExpRefList: List of data reference to tempExp
        \param[in] imageScalerList: List of image scalers
        \param[in] weightList: List of weights
        \param[in] altMaskList: List of alternate masks to use rather than those stored with tempExp, or None
        \param[in] statsFlags: afwMath.Property object for statistic for coadd
        \param[in] statsCtrl: Statistics control object for coadd
        """
        self.log.debug("Computing coadd over %s", bbox)
        bbox_grow = afwGeom.Box2I(bbox)
        bbox_grow.grow(self.config.bufferSize)
        for model in dcrModel:
            bbox_grow.clip(model.getBBox())
        tempExpName = self.getTempExpDatasetName(self.warpType)
        # coaddMaskedImage = coaddExposure.getMaskedImage()
        maskedImageList2 = []
        self.scale = None
        for tempExpRef, imageScaler, altMask in zip(tempExpRefList, imageScalerList, altMaskList):
            exposure = tempExpRef.get(tempExpName + "_sub", bbox=bbox_grow)
            visitInfo = exposure.getInfo().getVisitInfo()
            maskedImage = exposure.getMaskedImage()
            if self.scale is None:
                self.scale = exposure.getWcs().pixelScale()
            elif exposure.getWcs().pixelScale() != self.scale:
                self.log.warn("Incompatible pixel scale for %s %s", tempExpName, tempExpRef.dataId)

            if altMask:
                altMaskSub = altMask.Factory(altMask, bbox_grow, afwImage.PARENT)
                maskedImage.getMask().swap(altMaskSub)
            imageScaler.scaleMaskedImage(maskedImage)

            if self.config.removeMaskPlanes:
                mask = maskedImage.getMask()
                for maskPlane in self.config.removeMaskPlanes:
                    try:
                        mask &= ~mask.getPlaneBitMask(maskPlane)
                    except Exception as e:
                        self.log.warn("Unable to remove mask plane %s: %s", maskPlane, e.message)

            maskedImageCut = maskedImage.Factory(maskedImage, bbox=bbox)

            maskedImageList = self.dcrResiduals(dcrModel, maskedImageCut, visitInfo)
            maskedImageList2.append(maskedImageList)

        dcrSubModelOut = []
        with self.timer("stack"):
            for maskedImageList in maskedImageList2:
                dcrSubModelOut.append(afwMath.statisticsStack(
                    maskedImageList, statsFlags, statsCtrl, weightList))
        if self.config.doWeightGain:
            convergenceMetricNew = self.calculateConvergence(dcrModel, bbox, tempExpRefList, imageScalerList,
                                                             weightList, altMaskList)
            gain = convergenceMetric/convergenceMetricNew
            convergenceMetric = convergenceMetricNew
        else:
            gain = 1.
        self.conditionDcrModel(dcrModel, dcrSubModelOut, bbox_grow, gain=gain)

        for model, subModel in zip(dcrModel, dcrSubModelOut):
            model.assign(subModel, bbox)

    def calculateConvergence(self, dcrModel, bbox, tempExpRefList, imageScalerList,
                             weightList, altMaskList):
        tempExpName = self.getTempExpDatasetName(self.warpType)
        averageModel = numpy.mean(dcrModel, axis=0)
        count = 0
        metric = 0.
        for tempExpRef, imageScaler, altMask in zip(tempExpRefList, imageScalerList, altMaskList):
            exposure = tempExpRef.get(tempExpName + "_sub", bbox=bbox)
            visitInfo = exposure.getInfo().getVisitInfo()
            imageVals = exposure.getMaskedImage().getImage()[bbox].getArray()
            templateVals = self.buildMatchedTemplate(dcrModel, visitInfo)
            diffVals = numpy.abs(imageVals - templateVals)*averageModel
            refVals = numpy.abs(imageVals)*averageModel
            metric += numpy.sum(diffVals)/numpy.sum(refVals)
            count += 1
        return metric/count

    def dcrDivideCoadd(self, coaddExposure):
        dcrModel = [coaddExposure.getMaskedImage().clone() for f in range(self.config.dcrNSubbands)]
        for model in dcrModel:
            model.getImage().getArray()[:, :] /= self.config.dcrNSubbands
            model.getVariance().getArray()[:, :] /= self.config.dcrNSubbands
        return dcrModel

    def fillCoadd(self, dcrModel, skyInfo, tempExpRefList, weightList):
        for model in dcrModel:
            coaddExposure = afwImage.ExposureF(skyInfo.bbox, skyInfo.wcs)
            coaddExposure.setCalib(self.scaleZeroPoint.getCalib())
            coaddExposure.getInfo().setCoaddInputs(self.inputRecorder.makeCoaddInputs())
            self.assembleMetadata(coaddExposure, tempExpRefList, weightList)
            coaddUtils.setCoaddEdgeBits(model.getMask(), model.getVariance())
            coaddExposure.setMaskedImage(model)
            yield coaddExposure

    def convolveDcrModelPlane(self, dcrModelPlane, dcr, useInverse=False):
        if self.config.useFFT:
            raise NotImplementedError("The Fourier transform approach has not yet been written.")
        else:
            if useInverse:
                shift = (-dcr.dy, -dcr.dx)
            else:
                shift = (dcr.dy, dcr.dx)
            result = scipyShift(dcrModelPlane, shift)
        return result

    def conditionDcrModel(self, oldDcrModel, newDcrModel, bbox, gain=1.):
        for oldModel, newModel in zip(oldDcrModel, newDcrModel):
            newModel = (oldModel[bbox] + gain*newModel)/(1. + gain)

    def dcrShiftCalculate(self, visitInfo):
        rotation = visitInfo.getBoresightParAngle() + visitInfo.getBoresightRotAngle()

        dcr = namedtuple("dcr", ["dx", "dy"])
        for wl in self.wavelengthGenerator():
            # Note that refract_amp can be negative, since it's relative to the midpoint of the full band
            diffRefractAmp = differentialRefraction(wl, self.config.lambdaEff,
                                                    elevation=visitInfo.getBoresightAzAlt().getLatitude(),
                                                    observatory=visitInfo.getObservatory(),
                                                    weather=visitInfo.getWeather())
            diffRefractPix = diffRefractAmp.asArcseconds()/self.scale.asArcseconds()
            yield dcr(dx=diffRefractPix*numpy.sin(rotation.asRadians()),
                      dy=diffRefractPix*numpy.cos(rotation.asRadians()))

    def buildMatchedTemplate(self, dcrModel, visitInfo):
        dcrShift = self.dcrShiftCalculate(visitInfo)
        templateVals = numpy.sum([self.convolveDcrModelPlane(model, dcr)
                                  for dcr, model in zip(dcrShift, dcrModel)], axis=0)
        return templateVals

    def dcrResiduals(self, dcrModel, maskedImage, visitInfo, bbox):
        imageVals = maskedImage.getImage()[bbox].getArray()
        dcrShift = list(self.dcrShiftCalculate(visitInfo))
        shiftedModels = [self.convolveDcrModelPlane(model, dcr, useInverse=False)
                         for dcr, model in zip(dcrShift, dcrModel)]
        residualImages = []
        for f, dcr in enumerate(dcrShift):
            otherModelSum = numpy.zeros_like(imageVals)
            for f2 in range(self.config.dcrNSubbands):
                if f2 != f:
                    otherModelSum += shiftedModels[f2]
                residual = self.convolveDcrModelPlane(imageVals - otherModelSum, dcr, useInverse=True)
                residualImages.append(residual)
        return residualImages

    def wavelengthGenerator(self):
        wlRef = self.config.lambdaEff
        for wl in numpy.linspace(0., self.config.filterWidth, self.config.dcrNSubbands, endpoint=True):
            yield wlRef - self.config.filterWidth/2. + wl
