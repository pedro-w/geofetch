#!/usr/bin/env python
"""Fetch data and metdata from GEO and SRA.

This script will download GEO data (actually, raw SRA data) from SRA, given a
GEO accession. It wants a GSE number, which can be passed directly on the
command line, or you can instead provide a file with a list of GSE accessions.
By default it will download all the samples in that accession, but you can limit
this by creating a long-format file with GSM numbers specifying which individual
samples to include. If the second column is included, a third column may also be included and 
will be used as the sample_name; otherwise, the sample will be named according to the
GEO Sample_title field. Any columns after the third will be ignored.

The 1, 2, or 3-column input file would look like this:
GSE123	GSM####	Sample1
GSE123	GSM####	Sample2
GSE123	GSM####
GSE456

This will download 3 particular GSM experiments from GSE123, and everything from
GSE456. It will name the first two samples Sample1 and Sample2, and the third,
plus any from GSE456, will have names according to GEO metadata.

This script also produces an annotation metadata file for use as input to
alignment pipelines. By default, multiple Runs (SRR) in an Experiment (SRX) will
be treated as samples to combine, but this can be changed with a command-line
argument.

Metadata output:
For each GSE input accession (ACC),
- GSE_ACC#.soft a SOFT file (annotating the experiment itself)
- GSM_ACC#.soft a SOFT file (annotating the samples within the experiment)
- SRA_ACC#.soft a CSV file (annotating each SRA Run, retrieved from GSE->GSM->SRA)

In addition, a single combined metadata file ("annoComb") for the whole input,
including SRA and GSM annotations for each sample. Here, "combined" means that it will have
rows for every sample in every GSE included in your input. So if you just gave a single GSE,
then the combined file is the same as the GSE file. If any "merged" samples exist
(samples for which there are multiple SRR Runs for a single SRX Experiment), the
script will also produce a merge table CSV file with the relationships between
SRX and SRR.

The way this works: Starting from a GSE, select a subset of samples (GSM Accessions) provided, 
and then obtain the SRX identifier for each of these from GEO. Now, query SRA for these SRX 
accessions and get any associated SRR accessions. Finally, download all of these SRR data files.

"""

__author__ = "Nathan Sheffield"

# Outline:
# INPUT: A list of GSE ids, optionally including GSM ids to limit to.
# example: GSE61150
# 1. Grab SOFT file from
# http://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?targ=gsm&acc=GSE61150&form=text&view=full
# 2. parse it, produce a table with all needed fields.
# 3. Grab SRA values from field, use this link to grab SRA metadata:
# http://trace.ncbi.nlm.nih.gov/Traces/sra/sra.cgi?save=efetch&db=sra&rettype=runinfo&term=SRX079566
# http://trace.ncbi.nlm.nih.gov/Traces/sra/sra.cgi?save=efetch&db=sra&rettype=runinfo&term=SRP055171
# http://trace.ncbi.nlm.nih.gov/Traces/sra/sra.cgi?save=efetch&db=sra&rettype=runinfo&term=SRX883589

# 4. Parse the SRA RunInfo csv file and use the download_link field to grab the .sra file

from argparse import ArgumentParser
from collections import OrderedDict
import copy
import csv
import os
import os.path
import re
import subprocess
import sys
from utils import Accession


ANNOTATION_SHEET_KEYS = [
	"sample_name", "protocol", "read_type", "organism", "data_source",
	'Sample_title', 'Sample_source_name_ch1', 'Sample_organism_ch1', 
	"Sample_library_selection", "Sample_library_strategy", 
	'Sample_type',  "SRR", "SRX", 'Sample_geo_accession', 'Sample_series_id', 
	'Sample_instrument_model']


# Regex to parse out SRA accession identifiers
PROJECT_PATTERN = re.compile("(SRP\d{4,8})")
EXPERIMENT_PATTERN = re.compile("(SRX\d{4,8})")
GSE_PATTERN = re.compile("(GSE\d{4,8})")
SUPP_FILE_PATTERN = re.compile("Sample_supplementary_file")
SER_SUPP_FILE_PATTERN = re.compile("Series_supplementary_file")



def _parse_cmdl(cmdl):
	parser = ArgumentParser(description="Automatic GEO SRA data downloader")
	
	# Required
	parser.add_argument(
			"-i", "--input", dest="input", required=True,
			help="required: a GEO (GSE) accession, or a file with a list of GSE numbers")
	
	# Optional
	parser.add_argument(
			"-p",
			"--processed",
			default=False,
			action="store_true",
			help="By default, download raw data. Turn this flag to download processed data instead.")
	parser.add_argument(
			"-m", "--metadata",
			dest="metadata_folder",
			default=safe_echo("SRAMETA"),
			help="Specify a location to store metadata [Default: $SRAMETA:" + safe_echo("SRAMETA") + "]")
	
	parser.add_argument(
			"-b", "--bamfolder", dest="bam_folder", default=safe_echo("SRABAM"),
			help="Optional: Specify a location to store bam files [Default: $SRABAM:" + safe_echo("SRABAM") + "]")
	
	parser.add_argument(
			"-s", "--srafolder", dest="sra_folder", default=safe_echo("SRARAW"),
			help="Optional: Specify a location to store sra files [Default: $SRARAW:" + safe_echo("SRARAW") + "]")

	parser.add_argument(
			"-g", "--geofolder", default=safe_echo("GEODATA"),
			help="Optional: Specify a location to store processed GEO files [Default: $GEODATA:" + safe_echo("GEODATA") + "]")
	
	parser.add_argument(
			"--picard", dest="picard_path", default=safe_echo("PICARD"),
			help="Specify a path to the picard jar, if you want to convert fastq to bam [Default: $PICARD:" + safe_echo("PICARD") + "]")
	
	parser.add_argument(
			"--just-metadata", action="store_true",
			help="If set, don't actually run downloads, just create metadata")

	parser.add_argument(
			"-r", "--refresh-metadata", action="store_true",
			help="If set, re-download metadata even if it exists.")

	parser.add_argument(
			"--use-key-subset", action="store_true",
			help="Use just the keys defined in this module when writing out metadata.")

	parser.add_argument(
			"-x", "--split-experiments", action="store_true",
		help="By default, SRX experiments with multiple SRR Runs will be merged \
		in the metadata sheets. You can treat each run as a separate sample with \
		this argument.")
	
	return parser.parse_args(cmdl)



def parse_SOFT_line(l):
	"""
	Parse SOFT formatted line, returning a dictionary with the key-value pair.
	:param str l: A SOFT-formatted line to parse ( !key = value )
	:return dict[str, str]: A python Dict object representing the key-value.
	:raise InvalidSoftLineException: if given line can't be parsed as SOFT line
	"""
	elems = l[1:].split("=")
	return {elems[0].rstrip(): elems[1].lstrip()}



def write_annotation_sheet(gsm_metadata, file_annotation, use_key_subset=False):
	"""
	Write metadata sheet out as an annotation file.

	:param Mapping gsm_metadata: the data to write, parsed from a file
		with metadata/annotation information
	:param str file_annotation: the path to the file to write
	:param bool use_key_subset: whether to use the keys present in the
		metadata object given (False), or instead use a fixed set of keys
		defined within this module (True)
	"""
	print("  Writing sample annotation sheet:" + file_annotation)

	if use_key_subset:
		# Use complete data
		keys =  ANNOTATION_SHEET_KEYS
	else:
		keys = gsm_metadata[gsm_metadata.iterkeys().next()].keys()

	with open(file_annotation, 'wb') as of:
		w = csv.DictWriter(of, keys, extrasaction='ignore')
		w.writeheader()
		for item in gsm_metadata:
			w.writerow(gsm_metadata[item])



class InvalidSoftLineException(Exception):
	def __init__(self, l):
		super(self, "{}".format(l))



# From Jay@Stackoverflow
def which(program):
	"""Returns the path to a program to make sure it exists"""
	import os

	def is_exe(fp):
		return os.path.isfile(fp) and os.access(fp, os.X_OK)

	fpath, fname = os.path.split(program)
	if fpath:
		if is_exe(program):
			return program
	else:
		for path in os.environ["PATH"].split(os.pathsep):
			path = path.strip('"')
			exe_file = os.path.join(path, program)
			if is_exe(exe_file):
				return exe_file



def safe_echo(var):
	""" Returns an environment variable if it exists, or an empty string if not"""
	return os.getenv(var, "")



def update_columns(metadata, experiment_name, sample_name, read_type):
	"""
	Update the metadata associated with a particular experiment.
	
	For the experiment indicated, this function updates the value (mapping), 
	including new data and populating columns used by looper based on 
	existing values in the mapping.
	
	:param Mapping metadata: the key-value mapping to update
	:param str experiment_name: name of the experiment from which these
		data came and are associated; the key in the metadata mapping
		for which the value is to be updated
	:param str sample_name: name of the sample with which these data are
		associated
	:param str read_type: usually "single" or "paired," an indication of the
		type of sequencing reads for this experiment
	:return Mapping: 
	"""
	
	exp = metadata[experiment_name]

	# Protocol-agnostic
	exp["sample_name"] = sample_name
	exp["protocol"] = exp["Sample_library_selection"]
	exp["read_type"] = read_type
	exp["organism"] = exp['Sample_organism_ch1']
	exp["data_source"] = "SRA"
	exp["SRX"] = experiment_name

	# Protocol specified is lowercased prior to checking here to alleviate
	# dependence on case for the value in the annotations file.
	bisulfite_protocols = {"reduced representation": "RRBS", "random": "WGBS"}

	# Conditional on bisulfite sequencing
	# print(":" + exp["Sample_library_strategy"] + ":")
	# Try to be smart about some library methods, refining protocol if possible.
	if exp["Sample_library_strategy"] == "Bisulfite-Seq":
		print("Parsing protocol")
		proto = exp["Sample_library_selection"].lower()
		if proto in bisulfite_protocols:
			exp["protocol"] = bisulfite_protocols[proto]

	return exp


def main(cmdl):
	
	args = _parse_cmdl(cmdl)
	
	# Some sanity checks before proceeding
	if args.bam_folder and not which("samtools"):
		raise SystemExit("samtools not found")

	# Create a list of GSE accession numbers, either from file or a single value
	# from the command line
	# This will be a dict, with the GSE# as the key, and then each will have a list
	# of GSM# specifying the samples we're interested in from that GSE#. An empty
	# sample list means we should get all samples from that GSE#.
	# This loop will create this dict.
	acc_GSE_list = OrderedDict()
	
	if not os.path.isfile(args.input):
		print("Input: No file named {}; trying it as an accession...".format(args.input))
		# No limits accepted on command line, so keep an empty list.
		if args.input.startswith("SRP"):
			base, ext = os.path.splitext(args.input)
			if ext:
				raise ValueError("SRP-like input must be an SRP accession")
			file_sra = os.path.join(
				args.metadata_folder, "SRA_{}.csv".format(args.input))
			# Fetch and write the metdata for this SRP accession.
			Accession(args.input).fetch_metadata(file_sra)
			if args.just_metadata:
				return
			# Read the Run identifiers to download.
			run_ids = []
			with open(file_sra, 'r') as f:
				for l in f:
					if l.startswith("SRR"):
						r_id = l.split(",")[0]
						run_ids.append(r_id)
			print("{} run(s)".format(len(run_ids)))
			for r_id in run_ids:
				subprocess.call(['prefetch', r_id, '--max-size', '50000000'])
			# Early return if we've just handled SRP accession directly.
			return
		else:
			acc_GSE = args.input
			acc_GSE_list[acc_GSE] = OrderedDict()
	else:
		print("Input: Accession list file found: '{}'".format(args.input))
	
		# Read input file line by line.
		for line in open(args.input, 'r'):
			if (not line) or (line[0] in ["#", "\n", "\t"]):
				continue
			fields = [x.rstrip() for x in line.split("\t")]
			gse = fields[0]
			if not gse:
				continue
	
			gse = gse.rstrip()

			if len(fields) > 1:
				gsm = fields[1]
		
				if len(fields) > 2 and gsm != "":
					# There must have been a limit (GSM specified)
					# include a name if it doesn't already exist
					sample_name = fields[2].rstrip()
				else:
					sample_name = gsm
	
				if acc_GSE_list.has_key(gse):  # GSE already has a GSM; add the next one
					acc_GSE_list[gse][gsm] = sample_name
				else:
					acc_GSE_list[gse] = OrderedDict({gsm: sample_name})
			else:
				# No GSM limit; use empty dict.
				acc_GSE_list[gse] = {}
	
	
	# Loop through each accession.
	# This will process that accession, produce metadata and download files for
	# the GSM #s included in the list for each GSE#.
	# acc_GSE = "GSE61150" # example
	
	# This loop populates a list of metadata.
	metadata_dict = OrderedDict()
	combined_multi_table = []
	
	for acc_GSE in acc_GSE_list.keys():
		print("Processing accession: " + acc_GSE)
		if len(re.findall(GSE_PATTERN, acc_GSE)) != 1:
			print(len(re.findall(GSE_PATTERN, acc_GSE)))
			print("This does not appear to be a correctly formatted GSE accession! Continue anyway...")
	
		# Get GSM#s (away from sample_name)
		GSM_limit_list = acc_GSE_list[acc_GSE].keys() #[x[1] for x in acc_GSE_list[acc_GSE]]
	
		print("Limit to: " + str(acc_GSE_list[acc_GSE])) # a list of GSM#s
		print("Limit to: " + str(GSM_limit_list)) # a list of GSM#s
		if args.refresh_metadata:
			print("Refreshing metadata...")
		# For each GSE acc, produce a series of metadata files
		file_gse = os.path.join(args.metadata_folder, "GSE_" + acc_GSE + '.soft')
		file_gsm = os.path.join(args.metadata_folder, "GSM_" + acc_GSE + '.soft')
		file_multi = os.path.join(args.metadata_folder, "merge_" + acc_GSE + '.csv')
		file_sra = os.path.join(args.metadata_folder, "SRA_" + acc_GSE + '.csv')
		file_srafilt = os.path.join(args.metadata_folder, "SRA_" + acc_GSE + '_filt.csv')
	
	
		# Grab the GSE and GSM SOFT files from GEO.
		# The GSE file has metadata describing the experiment, which includes
		# The SRA number we need to download the raw data from SRA
		# The GSM file has metadata describing each sample, which we will use to
		# produce a sample annotation sheet.
		if not os.path.isfile(file_gse) or args.refresh_metadata:
			Accession(acc_GSE).fetch_metadata(file_gse)
		else:
			print("  Found previous GSE file: " + file_gse)
	
		if not os.path.isfile(file_gsm) or args.refresh_metadata:
			Accession(acc_GSE).fetch_metadata(file_gsm, typename="GSM")
		else:
			print("  Found previous GSM file: " + file_gsm)
	
		# A simple state machine to parse SOFT formatted files (Here, the GSM file)
		#gsm_metadata = {}
		gsm_metadata = OrderedDict()
		# For multi samples (samples with multiple runs), we keep track of these
		# relations in a separate table.
		gsm_multi_table = OrderedDict()
		# save the state
		current_sample_id = None
		current_sample_srx = False
		for line in open(file_gsm, 'r'):
			line = line.rstrip()
			if line[0] is "^":
				pl = parse_SOFT_line(line)
				if len(acc_GSE_list[acc_GSE]) > 0 and pl['SAMPLE'] not in GSM_limit_list:
					#sys.stdout.write("  Skipping " + a['SAMPLE'] + ".")
					current_sample_id = None
					continue
				current_sample_id = pl['SAMPLE']
				current_sample_srx = False
				gsm_metadata[current_sample_id] = {}
				sys.stdout.write ("  Found sample " + current_sample_id)
			elif current_sample_id is not None:
				pl = parse_SOFT_line(line)
				gsm_metadata[current_sample_id].update(pl)
	
				# For processed data, here's where we would download it
				if args.processed and not args.just_metadata:
					found = re.findall(SUPP_FILE_PATTERN, line)
					if found:
						print(pl[pl.keys()[0]])
	
				# Now convert the ids GEO accessions into SRX accessions
				if not current_sample_srx:
					found = re.findall(EXPERIMENT_PATTERN, line)
					if found:
						print(" (SRX accession: {})".format(found[0]))
						srx_id = found[0]
						gsm_metadata[srx_id] = gsm_metadata.pop(current_sample_id)
						gsm_metadata[srx_id]["gsm_id"] = current_sample_id  # save the GSM id
						current_sample_id = srx_id
						current_sample_srx = True
	
		# GSM SOFT file parsed, save it in a list
		metadata_dict[acc_GSE] = gsm_metadata
	
		# Parse out the SRA project identifier from the GSE file
		acc_SRP = None
		for line in open(file_gse, 'r'):
			found = re.findall(PROJECT_PATTERN, line)
			if found:
				acc_SRP = found[0]
				print("\n  Found SRA Project accession: {}\n".format(acc_SRP))
				break
			# For processed data, here's where we would download it
			if args.processed and not args.just_metadata:
				found = re.findall(SER_SUPP_FILE_PATTERN, line)
				if found:
					pl = parse_SOFT_line(line)
					file_url = pl[pl.keys()[0]].rstrip()
					print("File: " + str( file_url ))
					# download file
					if args.geofolder:
						data_folder = os.path.join(args.geofolder, acc_GSE)
						print(file_url, data_folder)
						subprocess.call(['wget', file_url, '-P', data_folder])

		if not acc_SRP:
			# If I can't get an SRA accession, maybe raw data wasn't submitted to SRA
			# as part of this GEO submission. Can't proceed.
			print("  \033[91mUnable to get SRA accession (SRP#) from GEO GSE SOFT file. No raw data?\033[0m")
			# but wait; another possibility: there's no SRP linked to the GSE, but there
			# could still be an SRX linked to the (each) GSM.
			if len(gsm_metadata) == 1:
				print("But the GSM has an SRX number; ")
				acc_SRP = gsm_metadata.keys()[0]
				print("Instead of an SRP, using SRX identifier for this sample:  " + acc_SRP)
			else:
				# More than one sample? not sure what to do here. Does this even happen?
				continue
	
		# Now we have an SRA number, grab the SraRunInfo Metadata sheet:
		# The SRARunInfo sheet has additional sample metadata, which we will combine
		# with the GSM file to produce a single sample a
		if not os.path.isfile(file_sra) or args.refresh_metadata:
			Accession(acc_SRP).fetch_metadata(file_sra)
		else:
			print("  Found previous SRA file: " + file_sra)
	
		print("SRP: {}".format(acc_SRP))
	
	
		# Parse metadata from SRA
		# Produce an annotated output from the GSM and SRARunInfo files.
		# This will merge the GSM and SRA sample metadata into a dict of dicts,
		# with one entry per sample.
		# NB: There may be multiple SRA Runs (and thus lines in the RunInfo file)
		# Corresponding to each sample.
		if not args.processed:
			file_read = open(file_sra, 'rb')
			file_write = open(file_srafilt, 'wb')
			print("Parsing SRA file to download SRR records")
			initialized = False
			
			input_file = csv.DictReader(file_read)
			for line in input_file:
				if not initialized:
					initialized = True
					w = csv.DictWriter(file_write, line.keys())
					w.writeheader()
				#print(line)
				#print(gsm_metadata[line['SampleName']])
				# SampleName is not necessarily the GSM number, though frequently it is
				#gsm_metadata[line['SampleName']].update(line)
	
				# Only download if it's in the include list:
				experiment = line["Experiment"]
				run_name = line["Run"]
				if experiment not in gsm_metadata:
					# print("Skipping: {}".format(experiment))
					continue
	
				# local convenience variable
				# possibly set in the input tsv file
				sample_name = None  # initialize to empty
				try:
					sample_name = acc_GSE_list[acc_GSE][gsm_metadata[experiment]["gsm_id"]]
				except KeyError:
					pass
				if not sample_name or sample_name is "":
					temp = gsm_metadata[experiment]['Sample_title']
					# Now do a series of transformations to cleanse the sample name
					temp = temp.replace(" ", "_")
					# Do people put commas in their sample names? Yes.
					temp = temp.replace(",", "_")
					temp = temp.replace("__", "_")
					sample_name = temp
	
				# Otherwise, record that there's SRA data for this run.
				# And set a few columns that are used as input to the Looper
				print("Updating columns for looper")
				update_columns(gsm_metadata, experiment, sample_name=sample_name, read_type=line['LibraryLayout'])

				# Some experiments are flagged in SRA as having multiple runs.
				if gsm_metadata[experiment].get("SRR") is not None:
					# This SRX number already has an entry in the table.
					print("  Found additional run: {} ({})".format(run_name, experiment))
	
					if isinstance(gsm_metadata[experiment]["SRR"], basestring) \
							and not gsm_multi_table.has_key(experiment):
						# Only one has been stuck in so far, make a list
						gsm_multi_table[experiment] = []
						# Add first the original one, which was stored as a string
						# previously
						gsm_multi_table[experiment].append(
							[sample_name, experiment, gsm_metadata[experiment]["SRR"]])
						# Now append the current SRR number in a list as [SRX, SRR]
						gsm_multi_table[experiment].append([sample_name, experiment, run_name])
					else:
						# this is the 3rd or later sample; the first two are done,
						# so just add it.
						gsm_multi_table[experiment].append([sample_name, experiment, run_name])
	
					if args.split_experiments:
						# Duplicate the gsm metadata for this experiment (copy to make sure
						# it's not just an alias).
						rep_number = len(gsm_multi_table[experiment])
						new_SRX = experiment + "_" + str(rep_number)
						gsm_metadata[new_SRX] = copy.copy(gsm_metadata[experiment])
						# gsm_metadata[new_SRX]["SRX"] = new_SRX
						gsm_metadata[new_SRX]["sample_name"] += "_" + str(rep_number)
						gsm_metadata[new_SRX]["SRR"] = run_name
					else:
						# Either way, set the srr code to multi in the main table.
						gsm_metadata[experiment]["SRR"] = "multi"
				else:
					# The first SRR for this SRX is added to GSM metadata
					gsm_metadata[experiment]["SRR"] = run_name
	
				#gsm_metadata[experiment].update(line)
	
				# Write to filtered SRA Runinfo file
				w.writerow(line)
				sys.stdout.write("Get SRR: {} ({})".format(run_name, experiment))
				bam_file = "" if args.bam_folder == "" else os.path.join(args.bam_folder, run_name + ".bam")
	
				# TODO: sam-dump has a built-in prefetch. I don't have to do
				# any of this stuff... This also solves the bad sam-dump issues.
	
				if os.path.exists(bam_file):
					print("  BAM found:" + bam_file)
				else:
					if not args.just_metadata:
						# Use the 'prefetch' utility from the SRA Toolkit
						# to download the raw reads.
						# (http://www.ncbi.nlm.nih.gov/books/NBK242621/)
						subprocess.call(['prefetch', run_name, '--max-size', '50000000'])
					else:
						print("  Dry run")
	
					if args.bam_folder is not '':
						print("  Converting to bam: " + run_name)
						sra_file = os.path.join(args.sra_folder, run_name + ".sra")
						if not os.path.exists(sra_file):
							print("SRA file doesn't exist, please download it first: " + sra_file)
							continue
	
						# The -u here allows unaligned reads, and seems to be
						# required for some sra files regardless of aligned state
						cmd = "sam-dump -u " + \
							  os.path.join(args.sra_folder, run_name + ".sra") + \
							  " | samtools view -bS - > " + bam_file
						#sam-dump -u SRR020515.sra | samtools view -bS - > test.bam
	
						print(cmd)
						subprocess.call(cmd, shell=True)
	
				# check to make sure it worked
				# NS: Sometimes sam-dump fails, yielding an empty bam file, but
				# a fastq-dump works. This happens on files with bad quality
				# encodings. I contacted GEO about it in December 2015
				# Here we check the file size and use fastq -> bam conversion
				# if the sam-dump failed.
				if args.bam_folder is not '':
					st = os.stat(bam_file)
					# print("File size: " + str(st.st_size))
					if st.st_size < 100:
						print("Bam conversion failed with sam-dump. Trying fastq-dump...")
						# recreate?
						cmd = "fastq-dump --split-3 -O " + \
							  os.path.realpath(args.sra_folder) + " " + \
							  os.path.join(args.sra_folder, run_name + ".sra")
						print(cmd)
						subprocess.call(cmd, shell=True)
						if not args.picard_path:
							print("Can't convert the fastq to bam without picard path")
						else:
							# was it paired data? you have to process it differently
							# so it knows it's paired end
							fastq0 = os.path.join(args.sra_folder, run_name + ".fastq")
							fastq1 = os.path.join(args.sra_folder, run_name + "_1.fastq")
							fastq2 = os.path.join(args.sra_folder, run_name + "_2.fastq")
		
							cmd = "java -jar " + args.picard_path + " FastqToSam"
							if os.path.exists(fastq1) and os.path.exists(fastq2):
								cmd += " FASTQ=" + fastq1
								cmd += " FASTQ2=" +  fastq2
							else:
								cmd += " FASTQ=" +  fastq0
							cmd += " OUTPUT=" + bam_file
							cmd += " SAMPLE_NAME=" + run_name
							cmd += " QUIET=true"
							print(cmd)
							subprocess.call(cmd, shell=True)
	
	
			file_read.close()
			file_write.close()
	
		# Print the per-GSE multi table
		if len(gsm_multi_table) > 0:
			multi_fw = open(file_multi, 'w')
			multiw = csv.writer(multi_fw, delimiter=",")
			for key, values in gsm_multi_table.items():
				print key, values
				multiw.writerows(values)
	
			multi_fw.close()
			print("  Wrote out a multi table: " + file_multi)
			combined_multi_table.append(gsm_multi_table)
	
	
	metadata_dict_combined = OrderedDict()
	for acc_GSE, gsm_metadata in metadata_dict.iteritems():
		file_annotation = os.path.join(args.metadata_folder, "anno_" + acc_GSE + '.csv')
		write_annotation_sheet(gsm_metadata, file_annotation, use_key_subset=args.use_key_subset)
		metadata_dict_combined.update(gsm_metadata)
	
	# Write combined annotation sheet
	
	out = os.path.splitext(os.path.basename(args.input))[0]
	file_annotation = os.path.join(args.metadata_folder, "annocomb_" + out + '.csv')
	write_annotation_sheet(metadata_dict_combined, file_annotation, use_key_subset=args.use_key_subset)
	
	# Write combined multi table
	
	if len(combined_multi_table) > 0:
	
		file_multi = os.path.join(args.metadata_folder, "allmerge_" + out + '.csv')
		multi_fw = open(file_multi, 'w')
		multiw = csv.writer(multi_fw, delimiter=",")
	
		mykeys = ["sample_name", "SRX",  "SRR"]
		multiw.writerow(mykeys)
	
		for multi_table in combined_multi_table:
			for key, values in multi_table.items():
				print key, values
				multiw.writerows(values)
	
		multi_fw.close()
		print("  All multi table: " + file_multi)



if __name__ == "__main__":
	main(sys.argv[1:])
