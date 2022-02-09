#!/usr/bin/env python3

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

import argparse
from collections import OrderedDict
import copy
import csv
import os
import re
import subprocess
import sys
# import tarfile
import time

from utils import Accession, parse_accessions, parse_SOFT_line
from _version import __version__

from logmuse import add_logging_options, logger_via_cli
from ubiquerg import expandpath, is_command_callable

_STRING_TYPES = str
_LOGGER = None

# A set of hard-coded keys if you want to limit to just a few instead of taking
# all information provided in GEO. Use with `--use-key-subset`
ANNOTATION_SHEET_KEYS = [
    "sample_name", "protocol", "read_type", "organism", "data_source",
    'Sample_title', 'Sample_source_name_ch1', 'Sample_organism_ch1',
    "Sample_library_selection", "Sample_library_strategy",
    'Sample_type', "SRR", "SRX", 'Sample_geo_accession', 'Sample_series_id',
    'Sample_instrument_model']

# Regex to parse out SRA accession identifiers
PROJECT_PATTERN = re.compile(r"(SRP\d{4,8})")
EXPERIMENT_PATTERN = re.compile(r"(SRX\d{4,8})")
GSE_PATTERN = re.compile(r"(GSE\d{4,8})")
SUPP_FILE_PATTERN = re.compile("Sample_supplementary_file")
SER_SUPP_FILE_PATTERN = re.compile("Series_supplementary_file")

SAMPLE_SUPP_METADATA_FILE = "_annotation_sample_processed.csv"
EXP_SUPP_METADATA_FILE = "_annotation_series_processed.csv"

# How many times should we retry failing prefetch call?
NUM_RETRIES = 3


class Geofetch:

    def __init__(self, args):
        self.args = args
        global _LOGGER
        _LOGGER = logger_via_cli(args, name="geofetch")
        self._LOGGER = _LOGGER
        # check to make sure prefetch is callable
        if not args.just_metadata and not args.processed:
            if not is_command_callable("prefetch"):
                raise SystemExit("You must first install the sratoolkit, with prefetch in your PATH.")

        if args.name:
            self.project_name = args.name
        else:
            self.project_name = os.path.splitext(os.path.basename(args.input))[0]

        if args.filter:
            self.filter_re = re.compile(args.filter.lower())
        else:
            self.filter_re = None

        if args.metadata_folder:
            self.metadata_expanded = expandpath(args.metadata_folder)
            if os.path.isabs(self.metadata_expanded):
                self.metadata_raw = args.metadata_folder
            else:
                self.metadata_expanded = os.path.abspath(self.metadata_expanded)
                self.metadata_raw = os.path.abspath(args.metadata_root)
            self.metadata_raw = args.metadata_folder
        else:
            self.metadata_expanded = expandpath(args.metadata_root)
            if os.path.isabs(self.metadata_expanded):
                self.metadata_raw = args.metadata_root
            else:
                self.metadata_expanded = os.path.abspath(self.metadata_expanded)
                self.metadata_raw = os.path.abspath(args.metadata_root)

            # Postpend the project name as a subfolder (only for -m option)
            self.metadata_expanded = os.path.join(self.metadata_expanded, self.project_name)
            self.metadata_raw = os.path.join(self.metadata_raw, self.project_name)

        self._LOGGER.info(f"Metadata folder: {self.metadata_expanded}")

        # Some sanity checks before proceeding
        if args.bam_conversion and not args.just_metadata and not self.which("samtools"):
            raise SystemExit("For SAM/BAM processing, samtools should be on PATH.")

        self.processed_metadata_samples = []
        self.processed_metadata_exp = []

    def run_geofetch(self):
        """ Main script driver/workflow """

        acc_GSE_list = parse_accessions(self.args.input, self.metadata_expanded, self.args.just_metadata)

        # Loop through each accession.
        # This will process that accession, produce metadata and download files for
        # the GSM #s included in the list for each GSE#.
        # acc_GSE = "GSE61150" # example

        # This loop populates a list of metadata.
        metadata_dict = OrderedDict()
        subannotation_dict = OrderedDict()
        failed_runs = []

        acc_GSE_keys = acc_GSE_list.keys()
        nkeys = len(acc_GSE_keys)
        ncount = 0
        for acc_GSE in acc_GSE_list.keys():
            ncount += 1
            if ncount <= self.args.skip:
                continue
            elif ncount == self.args.skip + 1:
                self._LOGGER.info(f"Skipped {self.args.skip} accessions. Starting now.")
            self._LOGGER.info(f"\033[38;5;228mProcessing accession {ncount} of {nkeys}: '{acc_GSE}'\033[0m")

            if len(re.findall(GSE_PATTERN, acc_GSE)) != 1:
                self._LOGGER.debug(len(re.findall(GSE_PATTERN, acc_GSE)))
                self._LOGGER.warning("This does not appear to be a correctly formatted GSE accession! "
                                     "Continue anyway...")

            if len(acc_GSE_list[acc_GSE]) > 0:
                self._LOGGER.info(f"Limit to: {list(acc_GSE_list[acc_GSE])}")  # a list of GSM#s

            if self.args.refresh_metadata:
                self._LOGGER.info("Refreshing metadata...")

            # For each GSE acc, produce a series of metadata files
            file_gse = os.path.join(self.metadata_expanded, acc_GSE + '_GSE.soft')
            file_gsm = os.path.join(self.metadata_expanded, acc_GSE + '_GSM.soft')
            file_sra = os.path.join(self.metadata_expanded, acc_GSE + '_SRA.csv')
            file_srafilt = os.path.join(self.metadata_expanded, acc_GSE + '_SRA_filt.csv')

            # Grab the GSE and GSM SOFT files from GEO.
            # The GSE file has metadata describing the experiment, which includes
            # The SRA number we need to download the raw data from SRA
            # The GSM file has metadata describing each sample, which we will use to
            # produce a sample annotation sheet.
            if not os.path.isfile(file_gse) or self.args.refresh_metadata:
                Accession(acc_GSE).fetch_metadata(file_gse)
            else:
                self._LOGGER.info(f"Found previous GSE file: {file_gse}")

            if not os.path.isfile(file_gsm) or self.args.refresh_metadata:
                Accession(acc_GSE).fetch_metadata(file_gsm, typename="GSM")
            else:
                self._LOGGER.info(f"Found previous GSM file: {file_gsm}")

            # download gsm metadata with processed files
            gsm_metadata = self.get_gsm_metadata(acc_GSE, acc_GSE_list,  file_gsm)
            metadata_dict[acc_GSE] = gsm_metadata

            self.get_SRA_meta(file_gse, file_sra, gsm_metadata)

            # download processed data
            if self.args.processed:
                data_geo_folder = os.path.join(self.args.geo_folder, acc_GSE)
                self._LOGGER.debug("Data folder: " + data_geo_folder)
                meta_processed_samples, meta_processed_series = self.get_list_of_processed_files(file_gse, file_gsm)
                try:
                    self.processed_metadata_samples.extend(meta_processed_samples)
                    self.processed_metadata_exp.extend(meta_processed_series)
                except:
                    pass
                processed_files = [each_file["file_url"] for each_file in meta_processed_samples]
                if not self.args.just_metadata:
                    for file_url in processed_files:
                        self.download_processed_file(file_url, data_geo_folder)


            # Parse metadata from SRA
            # Produce an annotated output from the GSM and SRARunInfo files.
            # This will merge the GSM and SRA sample metadata into a dict of dicts,
            # with one entry per sample.
            # NB: There may be multiple SRA Runs (and thus lines in the RunInfo file)
            # Corresponding to each sample.
            # For multi samples (samples with multiple runs), we keep track of these
            # relations in a separate table, which is called the subannotation table.
            gsm_multi_table = OrderedDict()
            if not self.args.processed:
                file_read = open(file_sra, 'r')
                file_write = open(file_srafilt, 'w')
                self._LOGGER.info("Parsing SRA file to download SRR records")
                initialized = False

                input_file = csv.DictReader(file_read)
                for line in input_file:
                    if not initialized:
                        initialized = True
                        wwrite = csv.DictWriter(file_write, line.keys())
                        wwrite.writeheader()

                    # Only download if it's in the include list:
                    experiment = line["Experiment"]
                    run_name = line["Run"]
                    if experiment not in gsm_metadata:
                        # print(f"Skipping: {experiment}")
                        continue

                    # local convenience variable
                    # possibly set in the input tsv file
                    sample_name = None  # initialize to empty
                    try:
                        sample_name = acc_GSE_list[acc_GSE][gsm_metadata[experiment]["gsm_id"]]
                    except KeyError:
                        pass
                    if not sample_name or sample_name == "":
                        temp = gsm_metadata[experiment]['Sample_title']
                        # Now do a series of transformations to cleanse the sample name
                        temp = temp.replace(" ", "_")
                        # Do people put commas in their sample names? Yes.
                        temp = temp.replace(",", "_")
                        temp = temp.replace("__", "_")
                        sample_name = temp

                    # Otherwise, record that there's SRA data for this run.
                    # And set a few columns that are used as input to the Looper
                    # print("Updating columns for looper")
                    self.update_columns(gsm_metadata, experiment, sample_name=sample_name,
                                        read_type=line['LibraryLayout'])

                    # Some experiments are flagged in SRA as having multiple runs.
                    if gsm_metadata[experiment].get("SRR") is not None:
                        # This SRX number already has an entry in the table.
                        self._LOGGER.info(f"Found additional run: {run_name} ({experiment})")

                        if isinstance(gsm_metadata[experiment]["SRR"], _STRING_TYPES) \
                                and experiment not in gsm_multi_table:
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

                        if self.args.split_experiments:
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

                    # gsm_metadata[experiment].update(line)

                    # Write to filtered SRA Runinfo file
                    wwrite.writerow(line)
                    self._LOGGER.info(f"Get SRR: {run_name} ({experiment})")
                    bam_file = "" if self.args.bam_folder == "" else os.path.join(self.args.bam_folder,
                                                                                  run_name + ".bam")
                    fq_file = "" if self.args.fq_folder == "" else os.path.join(self.args.fq_folder,
                                                                                run_name + "_1.fq")

                    # TODO: sam-dump has a built-in prefetch. I don't have to do
                    # any of this stuff... This also solves the bad sam-dump issues.

                    if os.path.exists(bam_file):
                        self._LOGGER.info(f"BAM found: {bam_file} . Skipping...")
                    elif os.path.exists(fq_file):
                        self._LOGGER.info(f"FQ found: {fq_file} .Skipping...")
                    else:
                        if not self.args.just_metadata:
                            try:
                                self.download_SRA_file(run_name)
                                # self._LOGGER.debug("skipped")
                            except Exception as err:
                                failed_runs.append(run_name)
                                self._LOGGER.warning(f"Error occurred while downloading SRA file: {err}")

                        else:
                            self._LOGGER.info("Dry run (no data download)")

                        if self.args.bam_conversion and self.args.bam_folder != '':
                            try:
                                # converting sra to bam using
                                self.sra_bam_conversion(bam_file, run_name)

                                # checking if bam_file converted correctly, if not --> use fastq-dump
                                st = os.stat(bam_file)
                                if st.st_size < 100:
                                    self._LOGGER.warning("Bam conversion failed with sam-dump. Trying fastq-dump...")
                                    self.sra_bam_conversion2(bam_file, run_name, self.args.picard_path)

                            except FileNotFoundError as err:
                                self._LOGGER.info(f"SRA file doesn't exist, please download it first: {err}")

                file_read.close()
                file_write.close()

            # accumulate subannotations
            subannotation_dict[acc_GSE] = gsm_multi_table

        # Combine individual accessions into project-level annotations, and write
        # individual accession files (if requested)
        metadata_dict_combined = OrderedDict()
        for acc_GSE, gsm_metadata in metadata_dict.items():
            file_annotation = os.path.join(self.metadata_expanded, acc_GSE + '_annotation.csv')
            if self.args.acc_anno:
                self.write_annotation(gsm_metadata, file_annotation, use_key_subset=self.args.use_key_subset)
            metadata_dict_combined.update(gsm_metadata)

        subannotation_dict_combined = OrderedDict()
        for acc_GSE, gsm_multi_table in subannotation_dict.items():
            file_subannotation = os.path.join(
                self.metadata_expanded, acc_GSE + '_subannotation.csv')
            if self.args.acc_anno:
                self.write_subannotation(gsm_multi_table, file_subannotation)
            subannotation_dict_combined.update(gsm_multi_table)

        # Logging additional information about processing
        self._LOGGER.info(f"Finished processing {len(acc_GSE_list)} accession(s)")

        if len(failed_runs) > 0:
            self._LOGGER.warn(f"The following samples could not be downloaded: {failed_runs}")

        # if user specified a pipeline interface path, add it into the project config
        if self.args.pipeline_interfaces:
            file_pipeline_interfaces = self.args.pipeline_interfaces
        else:
            file_pipeline_interfaces = "null"

        self._LOGGER.info("Creating complete project annotation sheets and config file...")
        # If the project included more than one GSE, we can now output combined
        # annotation tables for the entire project.

        # Write combined annotation sheet
        file_annotation = os.path.join(self.metadata_raw, self.project_name + '_annotation.csv')
        self.write_annotation(metadata_dict_combined, file_annotation, use_key_subset=self.args.use_key_subset)

        # Write combined subannotation table
        if len(subannotation_dict_combined) > 0:
            file_subannotation = os.path.join(
                self.metadata_raw, self.project_name + '_subannotation.csv')
            self.write_subannotation(subannotation_dict_combined, file_subannotation)
        else:
            file_subannotation = "null"

        # Write project config file

        if not self.args.config_template:
            geofetchdir = os.path.dirname(__file__)
            self.args.config_template = os.path.join(geofetchdir, "config_template.yaml")

        with open(self.args.config_template, 'r') as template_file:
            template = template_file.read()

        template_values = {
            "project_name": self.project_name,
            "annotation": os.path.basename(file_annotation),
            "subannotation": os.path.basename(file_subannotation),
            "pipeline_interfaces": file_pipeline_interfaces
        }

        for k, v in template_values.items():
            placeholder = "{" + str(k) + "}"
            template = template.replace(placeholder, str(v))

        config = os.path.join(self.metadata_raw, self.project_name + "_config.yaml")
        self._write(config, template, msg_pre="  Config file: ")

        if self.args.processed:
            # extend sample_characteristics_ch1
            for n_elem in range(len(self.processed_metadata_samples)):
                # TODO: make method out of this:: so it will be possible to separate many columns, not just one

                try:
                    if type(self.processed_metadata_samples[n_elem]["Sample_characteristics_ch1"]) is not list:
                        self.processed_metadata_samples[n_elem]["Sample_characteristics_ch1"] = [self.processed_metadata_samples[n_elem]["Sample_characteristics_ch1"]]
                    for elem in self.processed_metadata_samples[n_elem]["Sample_characteristics_ch1"]:
                        sample_char = dict([elem.split(": ")])
                        self.processed_metadata_samples[n_elem].update(sample_char)
                        del self.processed_metadata_samples[n_elem]["Sample_characteristics_ch1"]
                except KeyError as err:
                    self._LOGGER.warning("Key Error: %s" % err)
                except ValueError as err1:
                    self._LOGGER.warning("Value Error: %s" % err1)

            # adding missing keys to dict
            list_of_keys = []
            for element in self.processed_metadata_samples:
                list_of_keys.extend(list(element.keys()))

            list_of_keys = list(set(list_of_keys))

            for element in self.processed_metadata_samples:
                list_of_keys.extend(list(element.keys()))

            for k in list_of_keys:
                for list_elem in range(len(self.processed_metadata_samples)):
                    if k not in self.processed_metadata_samples[list_elem]:
                        self.processed_metadata_samples[list_elem][k] = ""

            # check if genome is ok:
            self.processed_metadata_samples = self.check_genome_value(self.processed_metadata_samples)


            mf_path = os.path.join(self.metadata_raw, self.project_name + SAMPLE_SUPP_METADATA_FILE)
            a_file = open(mf_path, "w")
            dict_writer = csv.DictWriter(a_file, self.processed_metadata_samples[0].keys())
            dict_writer.writeheader()
            dict_writer.writerows(self.processed_metadata_samples)
            a_file.close()


            list_of_keys = []
            for element in self.processed_metadata_exp:
                list_of_keys.extend(list(element.keys()))

            list_of_keys = list(set(list_of_keys))

            for element in self.processed_metadata_exp:
                list_of_keys.extend(list(element.keys()))

            for k in list_of_keys:
                for list_elem in range(len(self.processed_metadata_exp)):
                    if k not in self.processed_metadata_exp[list_elem]:
                        self.processed_metadata_exp[list_elem][k] = ""



            mf_path = os.path.join(self.metadata_raw, self.project_name + EXP_SUPP_METADATA_FILE)
            a_file = open(mf_path, "w")
            dict_writer = csv.DictWriter(a_file, self.processed_metadata_exp[0].keys())
            dict_writer.writeheader()
            dict_writer.writerows(self.processed_metadata_exp)
            a_file.close()

    def write_annotation(self, gsm_metadata, file_annotation, use_key_subset=False):
        """
        Write metadata sheet out as an annotation file.

        :param Mapping gsm_metadata: the data to write, parsed from a file
            with metadata/annotation information
        :param str file_annotation: the path to the file to write
        :param bool use_key_subset: whether to use the keys present in the
            metadata object given (False), or instead use a fixed set of keys
            defined within this module (True)
        :return str: path to file written
        """
        if use_key_subset:
            keys = ANNOTATION_SHEET_KEYS
        else:
            # keys = gsm_metadata[gsm_metadata.keys().next()].keys()
            keys = (list(list(gsm_metadata.values())[0].keys()))

        self._LOGGER.info(f"Sample annotation sheet: {file_annotation}")
        fp = expandpath(file_annotation)
        self._LOGGER.info(f"Writing: {fp}")
        with open(fp, 'w') as of:
            w = csv.DictWriter(of, keys, extrasaction='ignore')
            w.writeheader()
            for item in gsm_metadata:
                w.writerow(gsm_metadata[item])
        return fp

    def download_SRA_file(self, run_name):
        """
        Downloading SRA file by ising 'prefetch' utility from the SRA Toolkit
        more info: (http://www.ncbi.nlm.nih.gov/books/NBK242621/)
        :param str run_name: SRR number of the SRA file
        """

        # Set up a simple loop to try a few times in case of failure
        t = 0
        while True:
            t = t + 1
            subprocess_return = subprocess.call(['prefetch', run_name, '--max-size', '50000000'])

            if subprocess_return == 0:
                break

            if t >= NUM_RETRIES:
                raise RuntimeError(f"Prefetch retries of {run_name} failed. Try this sample later")

            self._LOGGER.info("Prefetch attempt failed, wait a few seconds to try again")
            time.sleep(t * 2)

    @staticmethod
    def which(program):
        """
        return str:  the path to a program to make sure it exists
        """
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

    def sra_bam_conversion(self, bam_file, run_name):
        """
        Converting of SRA file to BAM file by using samtools function "sam-dump"
        :param str bam_file: path to BAM file that has to be created
        :param str run_name: SRR number of the SRA file that has to be converted
        """
        self._LOGGER.info("Converting to bam: " + run_name)
        sra_file = os.path.join(self.args.sra_folder, run_name + ".sra")
        if not os.path.exists(sra_file):
            raise FileNotFoundError(sra_file)

        # The -u here allows unaligned reads, and seems to be
        # required for some sra files regardless of aligned state
        cmd = "sam-dump -u " + \
              os.path.join(self.args.sra_folder, run_name + ".sra") + \
              " | samtools view -bS - > " + bam_file
        # sam-dump -u SRR020515.sra | samtools view -bS - > test.bam

        self._LOGGER.info(f"Conversion command: {cmd}")
        subprocess.call(cmd, shell=True)

    @staticmethod
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
            # print("Parsing protocol")
            proto = exp["Sample_library_selection"].lower()
            if proto in bisulfite_protocols:
                exp["protocol"] = bisulfite_protocols[proto]

        return exp

    def sra_bam_conversion2(self, bam_file, run_name, picard_path=None):
        """
        Converting of SRA file to BAM file by using fastq-dump
        (is used when sam-dump fails, yielding an empty bam file. Here fastq -> bam conversion is used)
        :param str bam_file: path to BAM file that has to be created
        :param str run_name: SRR number of the SRA file that has to be converted
        :param str picard_path: Path to The Picard toolkit. More info: https://broadinstitute.github.io/picard/
        """

        # check to make sure it worked
        cmd = "fastq-dump --split-3 -O " + \
              os.path.realpath(self.args.sra_folder) + " " + \
              os.path.join(self.args.sra_folder, run_name + ".sra")
        self._LOGGER.info(f"Command: {cmd}")
        subprocess.call(cmd, shell=True)
        if not picard_path:
            self._LOGGER.warning("Can't convert the fastq to bam without picard path")
        else:
            # was it paired data? you have to process it differently
            # so it knows it's paired end
            fastq0 = os.path.join(self.args.sra_folder, run_name + ".fastq")
            fastq1 = os.path.join(self.args.sra_folder, run_name + "_1.fastq")
            fastq2 = os.path.join(self.args.sra_folder, run_name + "_2.fastq")

            cmd = "java -jar " + picard_path + " FastqToSam"
            if os.path.exists(fastq1) and os.path.exists(fastq2):
                cmd += " FASTQ=" + fastq1
                cmd += " FASTQ2=" + fastq2
            else:
                cmd += " FASTQ=" + fastq0
            cmd += " OUTPUT=" + bam_file
            cmd += " SAMPLE_NAME=" + run_name
            cmd += " QUIET=true"
            self._LOGGER.info(f"Conversion command: {cmd}")
            subprocess.call(cmd, shell=True)

    def write_subannotation(self, tabular_data, filepath, column_names=None):
        """
        Writes one or more tables to a given CSV filepath.

        :param tabular_data: Mapping | Iterable[Mapping]: single KV pair collection, or collection
            of such collections, to write to disk as tabular data
        :param str filepath: path to file to write, possibly with environment
            variables included, e.g. from a config file
        :param Iterable[str] column_names: collection of names for columns to
            write
        :return str: path to file written
        """
        self._LOGGER.info(f"Sample subannotation sheet: {filepath}")
        fp = expandpath(filepath)
        self._LOGGER.info(f"Writing: {fp}")
        with open(fp, 'w') as openfile:
            writer = csv.writer(openfile, delimiter=",")
            # write header
            writer.writerow(column_names or ["sample_name", "SRX", "SRR"])
            if not isinstance(tabular_data, list):
                tabular_data = [tabular_data]
            for table in tabular_data:
                for key, values in table.items():
                    self._LOGGER.debug(f"{key}: {values}")
                    writer.writerows(values)
        return fp

    def download_file(self, file_url, data_folder, sleep_after=0.5):
        """
        Given an url for a file, downloading to specified folder
        :param str file_url: the URL of the file to download
        :param str data_folder: path to the folder where data should be downloaded
        :param float sleep_after: time to sleep after downloading
        """
        filename = os.path.basename(file_url)
        full_filepath = os.path.join(data_folder, filename)
        if not os.path.exists(full_filepath):
            self._LOGGER.info(f"\033[38;5;242m")  # set color to gray
            ret = subprocess.call(['wget', '--no-clobber', file_url, '-P', data_folder])
            self._LOGGER.info(f"\033[38;5;242m{ret}\033[0m")
            time.sleep(sleep_after)
            self._LOGGER.info(f"\033[0m")  # Reset to default terminal color
        else:
            self._LOGGER.info(f"\033[38;5;242mFile {full_filepath} exists.\033[0m")

    def get_list_of_processed_files(self, file_gse, file_gsm):
        """
        Given a path to GSE metafile, downloading filelist of tar file and making list of all processed files
        :param str file_gse: the path to gse metafile
        :param str file_gsm: the path to gse metafile
        :return list: list of metadata of processed files
        """

        tar_re = re.compile(r".*\.tar$")
        gse_numb = None
        meta_processed_samples = []
        meta_processed_series = {"GSE": "", "files": []}
        for line in open(file_gse, 'r'):

            if re.compile(r"!Series_geo_accession").search(line):
                gse_numb = self.get_value(line)
                meta_processed_series["GSE"] = gse_numb
            found = re.findall(SER_SUPP_FILE_PATTERN, line)

            if found:
                pl = parse_SOFT_line(line)
                file_url = pl[list(pl.keys())[0]].rstrip()
                filename = os.path.basename(file_url)
                self._LOGGER.debug(f"Processed GSE file found: %s" % str(file_url))

                # search for tar file:
                if tar_re.search(filename):
                    nb = len(meta_processed_samples) - 1
                    for line_gsm in open(file_gsm, 'r'):
                        if line_gsm[0] == "^":
                            nb = len(self.check_file_existance(meta_processed_samples))
                            meta_processed_samples.append({"files": [],
                                                           "GSE": gse_numb
                                                           })
                        else:
                            pl = parse_SOFT_line(line_gsm.strip('\n'))
                            element_keys = list(pl.keys())[0]
                            element_values = list(pl.values())[0]
                            if not re.findall(SUPP_FILE_PATTERN, line_gsm):
                                if element_keys not in meta_processed_samples[nb].keys():
                                    meta_processed_samples[nb].update(pl)
                                else:
                                    if type(meta_processed_samples[nb][element_keys]) is not list:
                                        meta_processed_samples[nb][element_keys] = [meta_processed_samples[nb][element_keys]]
                                        meta_processed_samples[nb][element_keys].append(element_values)
                                    else:
                                        meta_processed_samples[nb][element_keys].append(element_values)

                        found_gsm = re.findall(SUPP_FILE_PATTERN, line_gsm)

                        if found_gsm:
                            pl = parse_SOFT_line(line_gsm)
                            file_url_gsm = pl[list(pl.keys())[0]].rstrip()
                            self._LOGGER.debug(f"Processed GSM file found: %s" % str(file_url_gsm))
                            if file_url_gsm != "NONE":
                                meta_processed_samples[nb]["files"].append(file_url_gsm)

                    self.check_file_existance(meta_processed_samples)
                    meta_processed_samples = self.separate_list_of_files(meta_processed_samples)
                    meta_processed_samples = self.separate_file_url(meta_processed_samples)

                    self._LOGGER.info(f"Total number of processed SAMPLES files found is: "
                                      f"%s" % str(len(meta_processed_samples)))
                    if self.filter_re:
                        meta_processed_samples = self.run_filter(meta_processed_samples)

                # other files than .tar
                else:
                    meta_processed_series["files"].append(file_url)

            # adding metadata to the experiment file
            try:
                bl = parse_SOFT_line(line.rstrip("\n"))
                bl_key = list(bl.keys())[0]
                bl_value = list(bl.values())[0]

                if bl_key not in meta_processed_series.keys():
                    meta_processed_series.update(bl)
                else:
                    if type(meta_processed_series[bl_key]) is not list:
                        meta_processed_series[bl_key] = [meta_processed_series[bl_key]]
                        meta_processed_series[bl_key].append(bl_value)
                    else:
                        meta_processed_series[bl_key].append(bl_value)
            except IndexError as ind_err:
                self._LOGGER.debug(f"IndexError in adding value to meta_processed_series: %s" % ind_err)

        meta_processed_series = self.separate_list_of_files(meta_processed_series)
        self._LOGGER.info(f"Total number of processed SERIES files found is: "
                          f"%s" % str(len(meta_processed_series)))
        if self.filter_re:
            meta_processed_series = self.run_filter(meta_processed_series)

        return meta_processed_samples, meta_processed_series


    @staticmethod
    def check_file_existance(meta_processed_sample):
        """
        Checking if last element of the list has files. If list of files is empty deleting it
        """
        nb = len(meta_processed_sample) - 1
        if nb > -1:
            if len(meta_processed_sample[nb]["files"]) == 0:
                del meta_processed_sample[nb]
                nb -= 1
        return meta_processed_sample

    @staticmethod
    def separate_list_of_files(meta_list, col_name="files"):
        """
        This method is separating list of files (dict value) or just simple dict
        into two different dicts
        """
        separated_list = []
        if type(meta_list) == list:
            for meta_elem in meta_list:
                for file_elem in meta_elem[col_name]:
                    new_dict = meta_elem.copy()
                    new_dict.pop(col_name, None)
                    new_dict["file"] = file_elem
                    separated_list.append(new_dict)
        elif type(meta_list) == dict:
            for file_elem in meta_list[col_name]:
                new_dict = meta_list.copy()
                new_dict.pop(col_name, None)
                new_dict["file"] = file_elem
                separated_list.append(new_dict)
        else:
            return TypeError("Incorrect type")

        return separated_list

    @staticmethod
    def separate_file_url(meta_list):
        """
        This method is adding dict key without file_name without path
        """
        separated_list = []
        for meta_elem in meta_list:
            new_dict = meta_elem.copy()
            new_dict["file_url"] = meta_elem["file"]
            new_dict["file"] = os.path.basename(meta_elem["file"])
            new_dict["sample_name"] = os.path.basename(meta_elem["file"])
            separated_list.append(new_dict)
        return separated_list


    def run_filter(self, meta_list, col_name="file"):
        """
        If user specified filter it will filter all this files here by col_name
        """
        filtered_list = []
        for meta_elem in meta_list:
            if self.filter_re.search(meta_elem[col_name].lower()):
                filtered_list.append(meta_elem)
        self._LOGGER.info("\033[32mTotal number of files after filter is: %i \033[0m" % len(filtered_list))

        return filtered_list


    def check_genome_value(self, meta_list):
        for elem_nb in range(len(meta_list)):
            try:
                if meta_list[elem_nb]["genome build"] == "" or  meta_list[elem_nb]["genome build"] is None:
                    for elem in meta_list[elem_nb]["Sample_data_processing"]:
                        dict_elem = elem.split(": ")
                        if dict_elem[0] == 'Genome_build':
                            meta_list[elem_nb]["genome build"] = dict_elem[-1]
                            break
            except KeyError:
                for elem_nb1 in range(len(meta_list)):
                    meta_list[elem_nb1]["genome build"] = ""
                self.check_genome_value(meta_list)
                break
            except Exception as err:
                self._LOGGER.warning(f"Error in check genome value: %s" % err)

        return meta_list

    @staticmethod
    def get_value(all_line):
        line_value = all_line.split("= ")[-1]
        return line_value.split(": ")[-1].rstrip("\n")

    def download_processed_file(self, file_url, data_folder):
        # TODO: change description
        """
        Given a url for a file, download it, and extract anything passing the filter.
        :param str file_url: the URL of the file to download
        :param str data_folder: the local folder where the file should be saved
        :return bool: True if the file is downloaded successfully; false if it does
        not pass filters and is not downloaded.

        # :param re.Pattern tar_re: a regulator expression (produced from re.compile)
        #    that pulls out filenames with .tar in them --- deleted
        # :param re.Pattern filter_re: a regular expression (produced from
        #    re.compile) to filter filenames of interest.
        """

        if not self.args.geo_folder:
            self._LOGGER.error("You must provide a geo_folder to download processed data.")
            sys.exit(1)

        filename = os.path.basename(file_url)
        ntry = 0

        while ntry < 10:
            try:
                self.download_file(file_url, data_folder)
                return True

            except IOError as e:
                self._LOGGER.error(str(e))
                # The server times out if we are hitting it too frequently,
                # so we should sleep a bit to reduce frequency
                sleeptime = (ntry + 1) ** 3
                self._LOGGER.info(f"Sleeping for {sleeptime} seconds")
                time.sleep(sleeptime)
                ntry += 1
                if ntry > 4:
                    raise e

    def get_SRA_meta(self, file_gse, file_sra, gsm_metadata):
        """
        Parse out the SRA project identifier from the GSE file

        :param str file_gse: full path to GSE.soft metafile
        :param str file_sra: full path to SRA.csv metafile that has to be downloaded
        :param OrderedDict gsm_metadata: dict of GSM metadata
        """
        #
        acc_SRP = None
        for line in open(file_gse, 'r'):
            found = re.findall(PROJECT_PATTERN, line)
            if found:
                acc_SRP = found[0]
                self._LOGGER.info(f"Found SRA Project accession: {acc_SRP}")
                break

        if not acc_SRP:
            # If I can't get an SRA accession, maybe raw data wasn't submitted to SRA
            # as part of this GEO submission. Can't proceed.
            self._LOGGER.warning("\033[91mUnable to get SRA accession (SRP#) from GEO GSE SOFT file. "
                                 "No raw data?\033[0m")
            # but wait; another possibility: there's no SRP linked to the GSE, but there
            # could still be an SRX linked to the (each) GSM.
            if len(gsm_metadata) == 1:
                try:
                    acc_SRP = gsm_metadata.keys()[0]
                    self._LOGGER.warning("But the GSM has an SRX number; instead of an "
                                         "SRP, using SRX identifier for this sample: " + acc_SRP)
                except TypeError:
                    self._LOGGER.warning("Error in gsm_metadata")

            # else:
            #     # More than one sample? not sure what to do here. Does this even happen?
            #     continue
        # Now we have an SRA number, grab the SraRunInfo Metadata sheet:
        # The SRARunInfo sheet has additional sample metadata, which we will combine
        # with the GSM file to produce a single sample a

        if not os.path.isfile(file_sra) or self.args.refresh_metadata:
            try:
                # downloading metadata
                Accession(acc_SRP).fetch_metadata(file_sra)
            except Exception as err:
                self._LOGGER.warning(f"\033[91mError occurred, while downloading SRA Info Metadata of {acc_SRP} ."
                                     f"Error: {err}  \033[0m")
        else:
            self._LOGGER.info("Found previous SRA file: " + file_sra)

        self._LOGGER.info(f"SRP: {acc_SRP}")

    def get_gsm_metadata(self, acc_GSE, acc_GSE_list, file_gsm):
        """
        A simple state machine to parse SOFT formatted files (Here, the GSM file)

        :param str acc_GSE: GSE number (Series accession)
        :param OrderedDict acc_GSE_list: list of GSE
        :param str file_gsm: full path to GSM.soft metafile
        :return OrderedDict: Ordered Dictionary of experiment information (gsm_metadata)
        """
        gsm_metadata = OrderedDict()

        # Get GSM#s (away from sample_name)
        GSM_limit_list = list(acc_GSE_list[acc_GSE].keys())

        # save the state
        current_sample_id = None
        current_sample_srx = False
        samples_list = []
        for line in open(file_gsm, 'r'):
            line = line.rstrip()
            if len(line) == 0:  # Apparently SOFT files can contain blank lines
                continue
            if line[0] == "^":
                pl = parse_SOFT_line(line)
                if len(acc_GSE_list[acc_GSE]) > 0 and pl['SAMPLE'] not in GSM_limit_list:
                    # sys.stdout.write("  Skipping " + a['SAMPLE'] + ".")
                    current_sample_id = None
                    continue
                current_sample_id = pl['SAMPLE']
                current_sample_srx = False
                columns_init = [("sample_name", ""), ("protocol", ""),
                                ("organism", ""), ("read_type", ""),
                                ("data_source", None), ("SRR", None), ("SRX", None)]
                gsm_metadata[current_sample_id] = OrderedDict(columns_init)

                self._LOGGER.debug(f"Found sample: {current_sample_id}")
                samples_list.append(current_sample_id)
            elif current_sample_id is not None:
                try:
                    pl = parse_SOFT_line(line)
                except IndexError:
                    # TODO: do we "fail the current sample" here and remove it
                    # from gsm_metadata? Or just skip the line?
                    self._LOGGER.debug(f"Failed to parse alleged SOFT line for sample ID {current_sample_id}; "
                                       f"line: {line}")
                    continue
                gsm_metadata[current_sample_id].update(pl)

                # Now convert the ids GEO accessions into SRX accessions
                if not current_sample_srx:
                    found = re.findall(EXPERIMENT_PATTERN, line)
                    if found:
                        self._LOGGER.debug(f"(SRX accession: {found[0]})")
                        srx_id = found[0]
                        gsm_metadata[srx_id] = gsm_metadata.pop(current_sample_id)
                        gsm_metadata[srx_id]["gsm_id"] = current_sample_id  # save the GSM id
                        current_sample_id = srx_id
                        current_sample_srx = True
        # GSM SOFT file parsed, save it in a list
        self._LOGGER.info(f"Processed {len(samples_list)} samples.")
        return gsm_metadata

    def _write(self, f_var_value, content, msg_pre=None, omit_newline=False):
        fp = expandpath(f_var_value)
        self._LOGGER.info((msg_pre or "") + fp)
        with open(fp, 'w') as f:
            f.write(content)
            if not omit_newline:
                f.write("\n")


def _parse_cmdl(cmdl):
    parser = argparse.ArgumentParser(description="Automatic GEO and SRA data downloader")

    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}")

    # Required
    parser.add_argument(
        "-i", "--input", dest="input", required=True,
        help="required: a GEO (GSE) accession, or a file with a list of GSE numbers")

    # Optional
    parser.add_argument(
        "-n", "--name",
        help="Specify a project name. Defaults to GSE number")

    parser.add_argument(
        "-m", "--metadata-root",
        dest="metadata_root",
        default=safe_echo("SRAMETA"),
        help="Specify a parent folder location to store metadata. "
             "The project name will be added as a subfolder "
             "[Default: $SRAMETA:" + safe_echo("SRAMETA") + "]")

    parser.add_argument(
        "-u", "--metadata-folder",
        help="Specify an absolute folder location to store metadata. "
             "No subfolder will be added. Overrides value of --metadata-root "
             "[Default: Not used (--metadata-root is used by default)]")

    parser.add_argument(
        "--just-metadata", action="store_true",
        help="If set, don't actually run downloads, just create metadata")

    parser.add_argument(
        "-r", "--refresh-metadata", action="store_true",
        help="If set, re-download metadata even if it exists.")

    parser.add_argument(
        "--acc-anno", action="store_true",
        help="Also produce annotation sheets for each accession, not just"
             " for the whole project combined")

    parser.add_argument(
        "--use-key-subset", action="store_true",
        help="Use just the keys defined in this module when writing out metadata.")

    parser.add_argument(
        "-x", "--split-experiments", action="store_true",
        help="""Split SRR runs into individual samples. By default, SRX
            experiments with multiple SRR Runs will have a single entry in the
            annotation table, with each run as a separate row in the
            subannotation table. This setting instead treats each run as a
            separate sample""")

    parser.add_argument(
        "--config-template", default=None,
        help="Project config yaml file template.")

    parser.add_argument(
        "-p", "--processed",
        default=False,
        action="store_true",
        help="Download processed data [Default: download raw data].")

    parser.add_argument(
        "-k", "--skip",
        default=0,
        type=int,
        help="Skip some accessions. [Default: no skip].")

    parser.add_argument(
        "--filter",
        default=None,
        help="Filter regex for processed filenames [Default: None].")

    parser.add_argument(
        "-g", "--geo-folder", default=safe_echo("GEODATA"),
        help="Optional: Specify a location to store processed GEO files "
             "[Default: $GEODATA:" + safe_echo("GEODATA") + "]")

    parser.add_argument(
        "-b", "--bam-folder", dest="bam_folder", default=safe_echo("SRABAM"),
        help="""Optional: Specify folder of bam files. Geofetch will not
            download sra files when corresponding bam files already exist.
            [Default: $SRABAM:""" + safe_echo("SRABAM") + "]")

    parser.add_argument(
        "-f", "--fq-folder", dest="fq_folder", default=safe_echo("SRAFQ"),
        help="""Optional: Specify folder of fastq files. Geofetch will not
            download sra files when corresponding fastq files already exist.
            [Default: $SRAFQ:""" + safe_echo("SRAFQ") + "]")

    parser.add_argument(
        "-P", "--pipeline_interfaces", default=None,
        help="Optional: Specify one or more filepaths to pipeline interface yaml files. "
             "These will be added to the project config file to make it immediately "
             "compatible with looper. [Default: null]")

    # Deprecated; these are for bam conversion which now happens in sra_convert
    # it still works here but I hide it so people don't use it, because it's confusing.
    parser.add_argument(
        "-s", "--sra-folder", dest="sra_folder", default=safe_echo("SRARAW"),
        help=argparse.SUPPRESS,
        # help="Optional: Specify a location to store sra files "
        #   "[Default: $SRARAW:" + safe_echo("SRARAW") + "]"
    )
    parser.add_argument(
        "--bam-conversion", action="store_true",
        # help="Turn on sequential bam conversion. Default: No conversion.",
        help=argparse.SUPPRESS)

    parser.add_argument(
        "--picard-path", dest="picard_path", default=safe_echo("PICARD"),
        # help="Specify a path to the picard jar, if you want to convert "
        # "fastq to bam [Default: $PICARD:" + safe_echo("PICARD") + "]",
        help=argparse.SUPPRESS)

    parser = add_logging_options(parser)
    return parser.parse_args(cmdl)


def safe_echo(var):
    """ Returns an environment variable if it exists, or an empty string if not"""
    return os.getenv(var, "")


class InvalidSoftLineException(Exception):
    """ Exception related to parsing SOFT line. """

    def __init__(self, l):
        """
        Create the exception by providing the problematic line.

        :param str l: the problematic SOFT line
        """
        super(self, f"{l}")


def main():
    """ Run the script. """
    args = _parse_cmdl(sys.argv[1:])
    Geofetch(args).run_geofetch()


if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        print("Pipeline aborted.")
        sys.exit(1)
