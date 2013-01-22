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

"""
This module contains a Task to register (align) multiple images.
"""

__all__ = ["RegisterTask",]

import math
import numpy

from lsst.pex.config import Config, Field, ConfigField
from lsst.pipe.base import Task, Struct
from lsst.meas.astrom.sip import makeCreateWcsWithSip
from lsst.afw.math import Warper

import lsst.afw.geom as afwGeom
import lsst.afw.table as afwTable


class RegisterConfig(Config):
    matchRadius = Field(dtype=float, default=1.0, doc="Matching radius (arcsec)")
    sipOrder = Field(dtype=int, default=4, doc="Order for SIP WCS", check=lambda x: x > 1)
    sipIter = Field(dtype=int, default=3, doc="Rejection iterations for SIP WCS", check=lambda x: x > 0)
    sipRej = Field(dtype=float, default=3.0, doc="Rejection threshold for SIP WCS", check=lambda x: x > 0)
    warper = ConfigField(dtype=Warper.ConfigClass, doc="Configuration for warping")


class RegisterTask(Task):
    ConfigClass = RegisterConfig

    def run(self, inputExp, templateExp, inputSources, templateSources):
        """Register (align) an input exposure to the template

        The exposures must have a relatively accurate Wcs to facilitate source
        matching within the 'matchRadius' in the configuration.

        The sources must have RA,Dec set.

        @param inputExp: Input exposure, to be aligned
        @param templateExp: Template exposure, serves as the target reference frame
        @param inputSources: Sources from input exposure
        @param templateSources: Sources from template exposure
        @return Struct(alignedExp: aligned input exposure,
                       alignedSources: aligned input sources,
                       wcs: Wcs that matches input to template,
                       )
        """

        inputWcs = inputExp.getWcs()
        templateWcs = templateExp.getWcs()

        matches = self.matchSources(inputSources, templateSources)
        newWcs = self.fitWcs(matches, inputExp)
        alignedExp = self.warpExposure(inputExp, newWcs, templateWcs, templateExp.getBBox())
        alignedSources = self.warpSources(inputSources, newWcs, templateWcs, templateExp.getBBox())
        return Struct(exp=alignedExp, sources=alignedSources, wcs=newWcs)

    def matchSources(self, inputSources, templateSources):
        # XXX Allow option to match by x,y (e.g., images are almost aligned but not quite)?
        matches = afwTable.matchRaDec(templateSources, inputSources,
                                      self.config.matchRadius*afwGeom.arcseconds)
        self.log.info("Matching within %.1f arcsec: %d matches" % (self.config.matchRadius, len(matches)))
        self.metadata.set("MATCH_NUM", len(matches))
        if len(matches) == 0:
            raise RuntimeError("Unable to match source catalogs")
        return matches

    def fitWcs(self, matches, inputExp):
        copyMatches = type(matches)(matches)
        refCoordKey = copyMatches[0].first.getTable().getCoordKey()
        inCentroidKey = copyMatches[0].second.getTable().getCentroidKey()
        for i in range(self.config.sipIter):
            sipFit = makeCreateWcsWithSip(copyMatches, inputExp.getWcs(), self.config.sipOrder,
                                          inputExp.getBBox())
            self.log.logdebug("Registration WCS RMS iteration %d: %f pixels" %
                              (i, sipFit.getScatterInPixels()))
            wcs = sipFit.getNewWcs()
            dr = [m.first.get(refCoordKey).angularSeparation(
                    wcs.pixelToSky(m.second.get(inCentroidKey))).asArcseconds() for
                  m in copyMatches]
            dr = numpy.array(dr)
            rms = math.sqrt((dr*dr).mean()) # RMS from zero
            rms = max(rms, 1.0e-9) # Don't believe any RMS smaller than this
            self.log.logdebug("Registration iteration %d: rms=%f" % (i, rms))
            good = numpy.where(dr < self.config.sipRej*rms)[0]
            numBad = len(copyMatches) - len(good)
            self.log.logdebug("Registration iteration %d: rejected %d" % (i, numBad))
            if numBad == 0:
                break
            copyMatches = type(matches)(copyMatches[i] for i in good)

        sipFit = makeCreateWcsWithSip(copyMatches, inputExp.getWcs(), self.config.sipOrder, inputExp.getBBox())
        self.log.info("Registration WCS: final WCS RMS=%f pixels from %d matches" % 
                      (sipFit.getScatterInPixels(), len(copyMatches)))
        self.metadata.set("SIP_RMS", sipFit.getScatterInPixels())
        self.metadata.set("SIP_GOOD", len(copyMatches))
        self.metadata.set("SIP_REJECTED", len(matches) - len(copyMatches))
        wcs = sipFit.getNewWcs()
        return wcs

    def warpExposure(self, inputExp, newWcs, templateWcs, templateBBox):
        warper = Warper.fromConfig(self.config.warper)

        copyExp = inputExp.Factory(inputExp.getMaskedImage(), newWcs)
        alignedExp = warper.warpExposure(templateWcs, copyExp, destBBox=templateBBox)
        # XXX warp PSF?
        # XXX anything else to transfer?

        return alignedExp

    def warpSources(self, inputSources, newWcs, templateWcs, templateBBox):
        """Warp sources to the new frame

        It would be difficult to transform all possible quantities of potential
        interest between the two frames.  We therefore update only the sky and
        pixel coordinates.
        """
        alignedSources = inputSources.copy(True)
        if not isinstance(templateBBox, afwGeom.Box2D):
            # There is no method Box2I::contains(Point2D)
            templateBBox = afwGeom.Box2D(templateBBox)
        table = alignedSources.getTable()
        coordKey = table.getCoordKey()
        centroidKey = table.getCentroidKey()
        centroidErrKey = table.getCentroidErrKey()
        deleteList = []
        for i, s in enumerate(alignedSources):
            oldCentroid = s.get(centroidKey)
            newCoord = newWcs.pixelToSky(oldCentroid)
            newCentroid = templateWcs.skyToPixel(newCoord)
            if not templateBBox.contains(newCentroid):
                deleteList.append(i)
                continue
            s.set(coordKey, newCoord)
            s.set(centroidKey, newCentroid)

        for i in reversed(deleteList): # Delete from back so we don't change indices
            del alignedSources[i]

        return alignedSources