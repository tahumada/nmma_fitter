# Copyright (C) 2006--2021  Kipp Cannon
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.


#
# =============================================================================
#
#                                   Preamble
#
# =============================================================================
#


import math
import sys


from ligo.lw import lsctables
from ligo.lw.utils import process as ligolw_process
from ligo.lw.utils import search_summary as ligolw_search_summary
from ligo import segments
from . import snglcluster


__author__ = "Kipp Cannon <kipp.cannon@ligo.org>"
from .git_version import date as __date__
from .git_version import version as __version__


#
# =============================================================================
#
#                           Add Process Information
#
# =============================================================================
#


process_program_name = "lalapps_bucluster"


def append_process(xmldoc, cluster_algorithm, comment):
	return ligolw_process.register_to_xmldoc(
		xmldoc,
		program = process_program_name,
		paramdict = {
			"cluster_algorithm": cluster_algorithm
		},
		version = __version__,
		cvs_repository = "lscsoft",
		cvs_entry_time = __date__,
		comment = comment
	)


#
# =============================================================================
#
#                        Add "Most Significant" Columns
#
# =============================================================================
#


#
# FIXME:  these columns should be generated by the C code, but that would
# change the sngl_burst table definition and interfere with the string
# search.  Something to sort out later.
#


def add_ms_columns(xmldoc):
	# add columns if required
	add_ms_columns_to_table(lsctables.SnglBurstTable.get_table(xmldoc))

def add_ms_columns_to_table(sngl_burst_table):
	added = False
	for colname in ("peak_frequency", "ms_start_time", "ms_start_time_ns", "ms_duration", "ms_flow", "ms_bandwidth", "ms_hrss", "ms_snr", "ms_confidence"):
		try:
			sngl_burst_table.getColumnByName(colname)
		except KeyError:
			sngl_burst_table.appendColumn(colname)
			added = True
	if not added:
		# didn't add any columns, so don't muck their contents
		return

	# at least one column was added, intialize them all
	for row in sngl_burst_table:
		row.peak_frequency = row.central_freq
		row.ms_period = row.period
		row.ms_band = row.band
		row.ms_hrss = row.amplitude
		row.ms_snr = row.snr
		row.ms_confidence = row.confidence


#
# =============================================================================
#
#                            Clustering Algorithms
#
# =============================================================================
#


#
# "excess power" clustering algorithm
#


def ExcessPowerPreFunc(sngl_burst_table):
	"""
	For speed, convert peak times to floats relative to epoch.
	"""
	if not len(sngl_burst_table):
		return
	offset = sngl_burst_table[0].peak
	for row in sngl_burst_table:
		row.peak_time = float(row.peak - offset)
	return offset



def ExcessPowerPostFunc(sngl_burst_table, offset):
	"""
	Restore peak times to absolute LIGOTimeGPS values.
	"""
	for row in sngl_burst_table:
		row.peak = offset + row.peak_time


def ExcessPowerSortKeyFunc(a):
	"""
	Sort key for grouping excess power triggers near triggers with
	which they might cluster.
	"""
	return (a.ifo, a.channel, a.search, a.start)


def ExcessPowerBailoutFunc(a, b):
	"""
	Returns True if a's and b's (ifo, channel, seach) are different or
	if the periods they span are disjoint.  Knowing excess power
	triggers have been ordered according to ExcessPowerSortKeyFunc(),
	then if for a pair of events this function returns False, we know
	the result will also be False for all other events farther apart in
	the list.  This is used to terminate the scan for events to
	cluster.
	"""
	return (a.ifo, a.channel, a.search) != (b.ifo, b.channel, b.search) or a.period.disjoint(b.period)


def ExcessPowerTestFunc(a, b):
	"""
	Return False if a and b cluster.  To cluster, two events must be
	from the same channel of the same instrument, and their
	time-frequency tiles must be non-disjoint.
	"""
	return (a.ifo, a.channel, a.search) != (b.ifo, b.channel, b.search) or a.period.disjoint(b.period) or a.band.disjoint(b.band)


def ExcessPowerClusterFunc(a, b):
	"""
	Modify a in place to be a cluster constructed from a and b.  The
	cluster's time-frequency tile is the smallest tile that contains
	the original two tiles, and the "most signficiant" contributor for
	the cluster is the tile whose boundaries are the SNR^{2} weighted
	average boundaries of the two contributing tiles.  The "most
	signficiant" contributor's h_{rss}, SNR, and confidence, are copied
	verbatim from whichever of the two contributing tiles has the
	highest confidence.  The modified event a is returned.
	"""
	#
	# In the special case of the two events being the exact same
	# time-frequency tile, simply preserve the one with the highest
	# confidence and discard the other.
	#

	if a.period == b.period and a.band == b.band:
		if b.ms_confidence > a.ms_confidence:
			return b
		return a

	#
	# Compute the properties of the "most significant contributor"
	#

	if b.ms_confidence > a.ms_confidence:
		a.ms_hrss = b.ms_hrss
		a.ms_snr = b.ms_snr
		a.ms_confidence = b.ms_confidence
	a.ms_period = snglcluster.weighted_average_seg(a.ms_period, a.snr**2.0, b.ms_period, b.snr**2.0)
	a.ms_band = snglcluster.weighted_average_seg(a.ms_band, a.snr**2.0, b.ms_band, b.snr**2.0)

	#
	# Compute the SNR squared weighted peak time and frequency (recall
	# that the peak times have been converted to floats relative to
	# epoch, and stored in the peak_time column).
	#

	a.peak_time = (a.snr**2.0 * a.peak_time + b.snr**2.0 * b.peak_time) / (a.snr**2.0 + b.snr**2.0)
	a.peak_frequency = (a.snr**2.0 * a.peak_frequency + b.snr**2.0 * b.peak_frequency) / (a.snr**2.0 + b.snr**2.0)

	#
	# Compute the combined h_rss and SNR by summing the original ones.
	# Note that no accounting of the overlap of the events is made, so
	# these parameters are being horribly overcounted, but the SNR in
	# particular must be summed like this in order to carry the
	# information needed to continue computing the SNR squared weighted
	# peak time and frequencies.
	#

	a.amplitude += b.amplitude
	a.snr = math.sqrt(a.snr**2.0 + b.snr**2.0)

	#
	# The confidence is the confidence of the most significant tile.
	#

	a.confidence = a.ms_confidence

	#
	# The cluster's frequency band is the smallest band containing the
	# bands of the two original events
	#

	a.band = snglcluster.smallest_enclosing_seg(a.band, b.band)

	#
	# The cluster's time interval is the smallest interval containing
	# the intervals of the two original events
	#

	a.period = snglcluster.smallest_enclosing_seg(a.period, b.period)

	#
	# Success
	#

	return a


def OmegaClusterFunc(a, b):
	"""
	Modify a in place to be a cluster constructed from a and b.  The
	cluster's time-frequency tile is the smallest tile that contains
	the original two tiles, and the "most signficiant" contributor for
	the cluster is the tile whose boundaries are the SNR^{2} weighted
	average boundaries of the two contributing tiles.  The "most
	signficiant" contributor's h_{rss}, SNR, and confidence, are copied
	verbatim from whichever of the two contributing tiles has the
	highest confidence.  The modified event a is returned.
	"""
	#
	# In the special case of the two events being the exact same
	# time-frequency tile, simply preserve the one with the highest
	# confidence and discard the other.
	#

	if a.period == b.period and a.band == b.band:
		if b.snr > a.snr:
			return b
		return a

	#
	# Compute the properties of the "most significant contributor"
	#

	if b.ms_snr > a.ms_snr:
		a.ms_snr = b.ms_snr
	a.ms_period = snglcluster.weighted_average_seg(a.ms_period, a.snr**2.0, b.ms_period, b.snr**2.0)
	a.ms_band = snglcluster.weighted_average_seg(a.ms_band, a.snr**2.0, b.ms_band, b.snr**2.0)

	#
	# Compute the SNR squared weighted peak time and frequency (recall
	# that the peak times have been converted to floats relative to
	# epoch, and stored in the peak_time column).
	#

	a.peak_time = (a.snr**2.0 * a.peak_time + b.snr**2.0 * b.peak_time) / (a.snr**2.0 + b.snr**2.0)
	a.peak_frequency = (a.snr**2.0 * a.peak_frequency + b.snr**2.0 * b.peak_frequency) / (a.snr**2.0 + b.snr**2.0)

	#
	# Compute the combined h_rss and SNR by summing the original ones.
	# Note that no accounting of the overlap of the events is made, so
	# these parameters are being horribly overcounted, but the SNR in
	# particular must be summed like this in order to carry the
	# information needed to continue computing the SNR squared weighted
	# peak time and frequencies.
	#

	a.amplitude += b.amplitude
	a.snr = math.sqrt(a.snr**2.0 + b.snr**2.0)

	#
	# The cluster's frequency band is the smallest band containing the
	# bands of the two original events
	#

	a.band = snglcluster.smallest_enclosing_seg(a.band, b.band)

	#
	# The cluster's time interval is the smallest interval containing
	# the intervals of the two original events
	#

	a.period = snglcluster.smallest_enclosing_seg(a.period, b.period)

	#
	# Success
	#

	return a

#
# =============================================================================
#
#                                 Library API
#
# =============================================================================
#


def bucluster(
	xmldoc,
	program,
	process,
	prefunc,
	postfunc,
	testfunc,
	clusterfunc,
	sortkeyfunc = None,
	bailoutfunc = None,
	verbose = False
):
	"""
	Run the clustering algorithm on the list of burst candidates.  The
	return value is the tuple (xmldoc, changed), where xmldoc is the
	input document, and changed is a boolean that is True if the
	contents of the sngl_burst table were altered, and False if the
	triggers were not modified by the clustering process.

	If the document does not contain a sngl_burst table, then the
	document is not modified (including no modifications to the process
	metadata tables).
	"""

	#
	# Extract live time segment and sngl_burst table
	#

	try:
		sngl_burst_table = lsctables.SnglBurstTable.get_table(xmldoc)
	except ValueError:
		# no-op:  document does not contain a sngl_burst table
		if verbose:
			print("document does not contain a sngl_burst table, skipping ...", file=sys.stderr)
		return xmldoc, False
	seglists = ligolw_search_summary.segmentlistdict_fromsearchsummary_out(xmldoc, program = program).coalesce()

	#
	# Preprocess candidates
	#

	if verbose:
		print("pre-processing ...", file=sys.stderr)
	preprocess_output = prefunc(sngl_burst_table)

	#
	# Cluster
	#

	table_changed = snglcluster.cluster_events(sngl_burst_table, testfunc, clusterfunc, sortkeyfunc = sortkeyfunc, bailoutfunc = bailoutfunc, verbose = verbose)

	#
	# Postprocess candidates
	#

	if verbose:
		print("post-processing ...", file=sys.stderr)
	postfunc(sngl_burst_table, preprocess_output)

	#
	# Update instrument list in process table and add search summary
	# information
	#

	process.instruments = seglists.keys()
	ligolw_search_summary.append_search_summary(xmldoc, process, inseg = seglists.extent_all(), outseg = seglists.extent_all(), nevents = len(sngl_burst_table))

	#
	# Done
	#

	return xmldoc, table_changed
