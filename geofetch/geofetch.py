#!/usr/bin/env python3

__author__ = ["Oleksandr Khoroshevskyi", "Vince Reuter", "Nathan Sheffield"]

import argparse
import copy
import csv
import os
import re
import sys
from string import punctuation
import requests
import xmltodict
from rich.progress import track
import yaml

# import tarfile
import time

from ._version import __version__
from .const import *
from .utils import (
    Accession,
    parse_accessions,
    parse_SOFT_line,
    convert_size,
    clean_soft_files,
    run_subprocess,
)

import logmuse
from ubiquerg import expandpath, is_command_callable
from io import StringIO
from typing import List, Union, Dict, Tuple, NoReturn
import peppy
import pandas as pd


class Geofetcher:
    """
    Class to download or get projects, metadata, data from GEO and SRA
    """

    def __init__(
        self,
        name: str = "",
        metadata_root: str = "",
        metadata_folder: str = "",
        just_metadata: bool = False,
        refresh_metadata: bool = False,
        config_template: str = None,
        pipeline_samples: str = None,
        pipeline_project: str = None,
        skip: int = 0,
        acc_anno: bool = False,
        use_key_subset: bool = False,
        processed: bool = False,
        data_source: str = "samples",
        filter: str = None,
        filter_size: str = None,
        geo_folder: str = ".",
        split_experiments: bool = False,
        bam_folder: str = "",
        fq_folder: str = "",
        sra_folder: str = "",
        bam_conversion: bool = False,
        picard_path: str = "",
        input: str = None,
        const_limit_project: int = 50,
        const_limit_discard: int = 250,
        attr_limit_truncate: int = 500,
        discard_soft: bool = False,
        add_dotfile: bool = False,
        disable_progressbar: bool = False,
        opts=None,
        **kwargs,
    ):

        if opts is not None:
            _LOGGER = logmuse.logger_via_cli(opts)
        else:
            _LOGGER = logmuse.init_logger(name="geofetch")

        self._LOGGER = _LOGGER

        if name:
            self.project_name = name
        else:
            try:
                self.project_name = os.path.splitext(os.path.basename(input))[0]
            except TypeError:
                self.project_name = "project_name"

        if metadata_folder:
            self.metadata_expanded = expandpath(metadata_folder)
            if os.path.isabs(self.metadata_expanded):
                self.metadata_root_full = metadata_folder
            else:
                self.metadata_expanded = os.path.abspath(self.metadata_expanded)
                self.metadata_root_full = os.path.abspath(metadata_root)
            self.metadata_root_full = metadata_folder
        else:
            self.metadata_expanded = expandpath(metadata_root)
            if os.path.isabs(self.metadata_expanded):
                self.metadata_root_full = metadata_root
            else:
                self.metadata_expanded = os.path.abspath(self.metadata_expanded)
                self.metadata_root_full = os.path.abspath(metadata_root)

        self.just_metadata = just_metadata
        self.refresh_metadata = refresh_metadata
        self.config_template = config_template

        # if user specified a pipeline interface path for samples, add it into the project config
        if pipeline_samples and pipeline_samples != "null":
            self.file_pipeline_samples = pipeline_samples
            self.file_pipeline_samples = (
                f"pipeline_interfaces: {self.file_pipeline_samples}"
            )
        else:
            self.file_pipeline_samples = ""

        # if user specified a pipeline interface path, add it into the project config
        if pipeline_project:
            self.file_pipeline_project = (
                f"looper:\n    pipeline_interfaces: {pipeline_project}"
            )
        else:
            self.file_pipeline_project = ""

        self.skip = skip
        self.acc_anno = acc_anno
        self.use_key_subset = use_key_subset
        self.processed = processed
        self.supp_by = data_source

        if filter:
            self.filter_re = re.compile(filter.lower())
        else:
            self.filter_re = None

            # Postpend the project name as a subfolder (only for -m option)
            self.metadata_expanded = os.path.join(
                self.metadata_expanded, self.project_name
            )
            self.metadata_root_full = os.path.join(self.metadata_root_full, self.project_name)

        if filter_size is not None:
            try:
                self.filter_size = convert_size(filter_size.lower())
            except ValueError as message:
                self._LOGGER.error(message)
                raise SystemExit()
        else:
            self.filter_size = filter_size

        self.geo_folder = geo_folder
        self.split_experiments = split_experiments
        self.bam_folder = bam_folder
        self.fq_folder = fq_folder
        self.sra_folder = sra_folder
        self.bam_conversion = bam_conversion
        self.picard_path = picard_path

        self.const_limit_project = const_limit_project
        self.const_limit_discard = const_limit_discard
        self.attr_limit_truncate = attr_limit_truncate

        self.discard_soft = discard_soft
        self.add_dotfile = add_dotfile
        self.disable_progressbar = disable_progressbar

        self._LOGGER.info(f"Metadata folder: {self.metadata_expanded}")

        # Some sanity checks before proceeding
        if bam_conversion and not just_metadata and not self._which("samtools"):
            raise SystemExit("For SAM/BAM processing, samtools should be on PATH.")

        self.just_object = False

    def get_project(
        self, input: str, just_metadata: bool = True, discard_soft: bool = True
    ) -> dict:
        """
        Function for fetching projects from GEO|SRA and receiving peppy project
        :param input: GSE number, or path to file of GSE numbers
        :param just_metadata: process only metadata
        :param discard_soft:  clean run, without downloading soft files
        :return: peppy project or list of project, if acc_anno is set.
        """
        self.just_metadata = just_metadata
        self.just_object = True
        self.disable_progressbar = True
        self.discard_soft = discard_soft
        acc_GSE_list = parse_accessions(
            input, self.metadata_expanded, self.just_metadata
        )

        project_dict = {}

        # processed data:
        if self.processed:
            if self.acc_anno:
                nkeys = len(acc_GSE_list.keys())
                ncount = 0
                self.acc_anno = False
                for acc_GSE in acc_GSE_list.keys():
                    ncount += 1
                    self._LOGGER.info(
                        f"\033[38;5;200mProcessing accession {ncount} of {nkeys}: '{acc_GSE}'\033[0m"
                    )
                    project_dict.update(self.fetch_all(input=acc_GSE, name=acc_GSE))
            else:
                project_dict.update(self.fetch_all(input=input, name="project"))

        # raw data:
        else:
            # Not sure about below code...
            if self.acc_anno:
                self.acc_anno = False
                nkeys = len(acc_GSE_list.keys())
                ncount = 0
                for acc_GSE in acc_GSE_list.keys():
                    ncount += 1
                    self._LOGGER.info(
                        f"\033[38;5;200mProcessing accession {ncount} of {nkeys}: '{acc_GSE}'\033[0m"
                    )
                    project = self.fetch_all(input=acc_GSE)
                    project_dict[acc_GSE + "_raw"] = project

            else:
                ser_dict = self.fetch_all(input=input)
                project_dict["raw_samples"] = ser_dict

        return project_dict

    def fetch_all(self, input: str, name: str = None):
        """Main script driver/workflow"""

        if name:
            self.project_name = name
        else:
            try:
                self.project_name = os.path.splitext(os.path.basename(input))[0]
            except TypeError:
                self.project_name = input

        # check to make sure prefetch is callable
        if not self.just_metadata and not self.processed:
            if not is_command_callable("prefetch"):
                raise SystemExit(
                    "To download raw data You must first install the sratoolkit, with prefetch in your PATH."
                    " Installation instruction: http://geofetch.databio.org/en/latest/install/"
                )

        acc_GSE_list = parse_accessions(
            input, self.metadata_expanded, self.just_metadata
        )

        metadata_dict_combined = {}
        subannotation_dict_combined = {}

        processed_metadata_samples = []
        processed_metadata_series = []

        acc_GSE_keys = acc_GSE_list.keys()
        nkeys = len(acc_GSE_keys)
        ncount = 0
        for acc_GSE in track(
            acc_GSE_list.keys(),
            description="Processing... ",
            disable=self.disable_progressbar,
        ):

            ncount += 1
            if ncount <= self.skip:
                continue
            elif ncount == self.skip + 1:
                self._LOGGER.info(f"Skipped {self.skip} accessions. Starting now.")

            if not self.just_object:
                self._LOGGER.info(
                    f"\033[38;5;200mProcessing accession {ncount} of {nkeys}: '{acc_GSE}'\033[0m"
                )

            if len(re.findall(GSE_PATTERN, acc_GSE)) != 1:
                self._LOGGER.debug(len(re.findall(GSE_PATTERN, acc_GSE)))
                self._LOGGER.warning(
                    "This does not appear to be a correctly formatted GSE accession! "
                    "Continue anyway..."
                )

            if len(acc_GSE_list[acc_GSE]) > 0:
                self._LOGGER.info(
                    f"Limit to: {list(acc_GSE_list[acc_GSE])}"
                )  # a list of GSM#s

            # For each GSE acc, produce a series of metadata files
            file_gse = os.path.join(self.metadata_expanded, acc_GSE + "_GSE.soft")
            file_gsm = os.path.join(self.metadata_expanded, acc_GSE + "_GSM.soft")
            file_sra = os.path.join(self.metadata_expanded, acc_GSE + "_SRA.csv")

            if not os.path.isfile(file_gse) or self.refresh_metadata:
                file_gse_content = Accession(acc_GSE).fetch_metadata(
                    file_gse, clean=self.discard_soft
                )
            else:
                self._LOGGER.info(f"Found previous GSE file: {file_gse}")
                gse_file_obj = open(file_gse, "r")
                file_gse_content = gse_file_obj.read().split("\n")
                file_gse_content = [elem for elem in file_gse_content if len(elem) > 0]

            if not os.path.isfile(file_gsm) or self.refresh_metadata:
                file_gsm_content = Accession(acc_GSE).fetch_metadata(
                    file_gsm, typename="GSM", clean=self.discard_soft
                )
            else:
                self._LOGGER.info(f"Found previous GSM file: {file_gsm}")
                gsm_file_obj = open(file_gsm, "r")
                file_gsm_content = gsm_file_obj.read().split("\n")
                file_gsm_content = [elem for elem in file_gsm_content if len(elem) > 0]

            gsm_enter_dict = acc_GSE_list[acc_GSE]

            # download processed data
            if self.processed:
                (
                    meta_processed_samples,
                    meta_processed_series,
                ) = self.fetch_processed_one(
                    gse_file_content=file_gse_content,
                    gsm_file_content=file_gsm_content,
                    gsm_filter_list=gsm_enter_dict,
                )

                # download processed files:
                if not self.just_metadata:
                    self._download_processed_data(
                        acc_gse=acc_GSE,
                        meta_processed_samples=meta_processed_samples,
                        meta_processed_series=meta_processed_series,
                    )

                # generating PEPs for processed files:
                if self.acc_anno:
                    self._generate_processed_meta(
                        acc_GSE, meta_processed_samples, meta_processed_series
                    )

                else:
                    # adding metadata from current experiment to the project
                    processed_metadata_samples.extend(meta_processed_samples)
                    processed_metadata_series.extend(meta_processed_series)

            else:
                # read gsm metadata
                gsm_metadata = self._read_gsm_metadata(
                    acc_GSE, acc_GSE_list, file_gsm_content
                )

                # download sra metadata
                srp_list_result = self._get_SRA_meta(
                    file_gse_content, gsm_metadata, file_sra
                )
                if not srp_list_result:
                    self._LOGGER.info(f"No SRP data, continuing ....")
                    self._LOGGER.warning(f"No raw pep will be created! ....")
                    # delete current acc if no raw data was found
                    # del metadata_dict[acc_GSE]
                    pass
                else:
                    self._LOGGER.info("Parsing SRA file to download SRR records")
                gsm_multi_table = self._process_sra_meta(
                    srp_list_result, gsm_enter_dict, gsm_metadata
                )

                # download raw data:
                if not self.just_metadata:
                    for file_key in gsm_multi_table.keys():
                        for run in gsm_multi_table[file_key]:
                            # download raw data
                            self._LOGGER.info(
                                f"Getting SRR: {run[2]}  in ({acc_GSE})"
                            )
                            self._download_raw_data(run[2])
                else:
                    self._LOGGER.info(f"Dry run, no data will be downloaded")

                # save one project
                if self.acc_anno and nkeys > 1:
                    self._write_raw_annotation_new(name=acc_GSE, metadata_dict=gsm_metadata, subannot_dict=gsm_multi_table)

                else:
                    metadata_dict_combined.update(gsm_metadata)
                    subannotation_dict_combined.update(gsm_multi_table)

        self._LOGGER.info(f"Finished processing {len(acc_GSE_list)} accession(s)")

        # Logging cleaning process:
        if self.discard_soft:
            self._LOGGER.info(f"Cleaning soft files ...")
            clean_soft_files(self.metadata_root_full)

        #######################################################################################

        # saving PEPs for processed data
        if self.processed:
            if not self.acc_anno:
                return_value = self._generate_processed_meta(
                    name="PEP_processed",
                    meta_processed_samples=processed_metadata_samples,
                    meta_processed_series=processed_metadata_series,
                )
                if self.just_object:
                    return return_value

        # saving PEPs for raw data
        else:
            return_value = self._write_raw_annotation_new("PEP", metadata_dict_combined, subannotation_dict_combined)
            if self.just_object:
                return return_value

    def _process_sra_meta(self, srp_list_result=None, gsm_enter_dict=None, gsm_metadata=None):
        gsm_multi_table = {}
        for line in srp_list_result:

            # Only download if it's in the include list:
            experiment = line["Experiment"]
            run_name = line["Run"]
            if experiment not in gsm_metadata:
                # print(f"Skipping: {experiment}")
                continue

            sample_name = None
            try:
                sample_name = gsm_enter_dict[gsm_metadata[experiment]["gsm_id"]]
            except KeyError:
                # No name in input file
                pass

            if not sample_name or sample_name == "":
                temp = gsm_metadata[experiment]["Sample_title"]
                sample_name = self._sanitize_name(temp)

            # Otherwise, record that there's SRA data for this run.
            # And set a few columns that are used as input to the Looper
            # print("Updating columns for looper")
            self._update_columns(
                gsm_metadata,
                experiment,
                sample_name=sample_name,
                read_type=line["LibraryLayout"],
            )

            # Some experiments are flagged in SRA as having multiple runs.
            if gsm_metadata[experiment].get("SRR") is not None:
                # This SRX number already has an entry in the table.
                self._LOGGER.debug(f"Found additional run: {run_name} ({experiment})")
                if (
                    isinstance(gsm_metadata[experiment]["SRR"], str)
                    and experiment not in gsm_multi_table
                ):
                    gsm_multi_table[experiment] = []

                    gsm_multi_table[experiment].append(
                        [
                            sample_name,
                            experiment,
                            gsm_metadata[experiment]["SRR"],
                        ]
                    )
                    gsm_multi_table[experiment].append(
                        [sample_name, experiment, run_name]
                    )
                else:
                    gsm_multi_table[experiment].append(
                        [sample_name, experiment, run_name]
                    )

                if self.split_experiments:
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

        return gsm_multi_table

    def _download_raw_data(self, run_name):
        bam_file = (
            ""
            if self.bam_folder == ""
            else os.path.join(self.bam_folder, run_name + ".bam")
        )
        fq_file = (
            ""
            if self.fq_folder == ""
            else os.path.join(self.fq_folder, run_name + "_1.fq")
        )

        if os.path.exists(bam_file):
            self._LOGGER.info(f"BAM found: {bam_file} . Skipping...")
        elif os.path.exists(fq_file):
            self._LOGGER.info(f"FQ found: {fq_file} .Skipping...")
        else:
            try:
                self._download_SRA_file(run_name)
            except Exception as err:
                self._LOGGER.warning(
                    f"Error occurred while downloading SRA file: {err}"
                )

            if self.bam_conversion and self.bam_folder != "":
                try:
                    # converting sra to bam using
                    # TODO: sam-dump has a built-in prefetch. I don't have to do
                    # any of this stuff... This also solves the bad sam-dump issues.
                    self._sra_bam_conversion(bam_file, run_name)

                    # checking if bam_file converted correctly, if not --> use fastq-dump
                    st = os.stat(bam_file)
                    if st.st_size < 100:
                        self._LOGGER.warning(
                            "Bam conversion failed with sam-dump. Trying fastq-dump..."
                        )
                        self._sra_bam_conversion2(bam_file, run_name, self.picard_path)

                except FileNotFoundError as err:
                    self._LOGGER.info(
                        f"SRA file doesn't exist, please download it first: {err}"
                    )

    def fetch_processed_one(
        self,
        gse_file_content: list,
        gsm_file_content: list,
        gsm_filter_list: dict,
    ) -> Tuple:
        """
        Fetching just one processed GSE project
        :param gsm_file_content: gse soft file content
        :param gse_file_content: gsm soft file content
        :param gsm_filter_list: list of gsm that have to be downloaded
        :return: Tuple of project list of gsm samples and gse samples
        """
        (
            meta_processed_samples,
            meta_processed_series,
        ) = self._get_list_of_processed_files(gse_file_content, gsm_file_content)

        # taking into account list of GSM that is specified in the input file
        meta_processed_samples = self._filter_gsm(
            meta_processed_samples, gsm_filter_list
        )

        # samples
        meta_processed_samples = self._expand_metadata_list(meta_processed_samples)

        # series
        meta_processed_series = self._expand_metadata_list(meta_processed_series)

        # convert column names to lowercase and underscore
        meta_processed_samples = self._standardize_colnames(meta_processed_samples)
        meta_processed_series = self._standardize_colnames(meta_processed_series)

        return meta_processed_samples, meta_processed_series

    def _generate_processed_meta(
        self, name: str, meta_processed_samples: list, meta_processed_series: list
    ) -> dict:
        """
        Generate and save PEPs for processed accessions. GEO has data in GSE and GSM,
            conditions are used to decide which PEPs have to be saved.
        :param name: name of the folder/file where PEP will be saved
        :param meta_processed_samples:
        :param meta_processed_series:
        :return: dict of objects if just_object is set, otherwise dicts of None
        """
        return_objects = {f"{name}_samples": None, f"{name}_series": None}

        if self.supp_by == "all":
            # samples
            pep_acc_path_sample = os.path.join(
                self.metadata_root_full,
                f"{name}_samples",
                name + SAMPLE_SUPP_METADATA_FILE,
            )
            return_objects[f"{name}_samples"] = self._write_processed_annotation(
                meta_processed_samples,
                pep_acc_path_sample,
                just_object=self.just_object,
            )

            # series
            pep_acc_path_exp = os.path.join(
                self.metadata_root_full,
                f"{name}_series",
                name + EXP_SUPP_METADATA_FILE,
            )
            return_objects[f"{name}_series"] = self._write_processed_annotation(
                meta_processed_series,
                pep_acc_path_exp,
                just_object=self.just_object,
            )

        elif self.supp_by == "samples":
            pep_acc_path_sample = os.path.join(
                self.metadata_root_full,
                f"{name}_samples",
                name + SAMPLE_SUPP_METADATA_FILE,
            )
            return_objects[f"{name}_samples"] = self._write_processed_annotation(
                meta_processed_samples,
                pep_acc_path_sample,
                just_object=self.just_object,
            )
        elif self.supp_by == "series":
            return_objects[f"{name}_series"] = pep_acc_path_exp = os.path.join(
                self.metadata_root_full,
                f"{name}_series",
                name + EXP_SUPP_METADATA_FILE,
            )
            self._write_processed_annotation(
                meta_processed_series,
                pep_acc_path_exp,
                just_object=self.just_object,
            )

        return return_objects

    def _download_processed_data(
        self, acc_gse: str, meta_processed_samples: list, meta_processed_series: list
    ) -> NoReturn:
        data_geo_folder = os.path.join(self.geo_folder, acc_gse)
        self._LOGGER.debug("Data folder: " + data_geo_folder)

        if self.supp_by == "all":
            processed_samples_files = [
                each_file["file_url"] for each_file in meta_processed_samples
            ]
            for file_url in processed_samples_files:
                self._download_processed_file(file_url, data_geo_folder)

            processed_series_files = [
                each_file["file_url"] for each_file in meta_processed_series
            ]
            for file_url in processed_series_files:
                self._download_processed_file(file_url, data_geo_folder)

        elif self.supp_by == "samples":
            processed_samples_files = [
                each_file["file_url"] for each_file in meta_processed_samples
            ]
            for file_url in processed_samples_files:
                self._download_processed_file(file_url, data_geo_folder)

        elif self.supp_by == "series":
            processed_series_files = [
                each_file["file_url"] for each_file in meta_processed_series
            ]
            for file_url in processed_series_files:
                self._download_processed_file(file_url, data_geo_folder)

    def _expand_metadata_list_in_dict(self, metadata_dict: dict) -> dict:
        prj_list = self._dict_to_list_convector(proj_dict=metadata_dict)
        prj_list = self._expand_metadata_list(prj_list)
        return self._dict_to_list_convector(proj_list=prj_list)

    def _expand_metadata_list(self, metadata_list: list) -> list:
        """
        Expanding all lists of all items in the list by creating new items or joining them

        :param list metadata_list: list of dicts that store metadata
        :return list: expanded metadata list
        """
        self._LOGGER.info("Expanding metadata list...")
        list_of_keys = self._get_list_of_keys(metadata_list)
        for key_in_list in list_of_keys:
            metadata_list = self._expand_metadata_list_item(metadata_list, key_in_list)
        return metadata_list

    def _expand_metadata_list_item(self, metadata_list: list, dict_key: str):
        """
        Expanding list of one element (item) in the list by creating new items or joining them
        ["first1: fff", ...] -> separate columns

        :param list metadata_list: list of dicts that store metadata
        :param str dict_key: key in the dictionaries that have to be expanded
        :return list: expanded metadata list
        """
        try:
            element_is_list = any(
                type(list_item.get(dict_key)) is list for list_item in metadata_list
            )
            if element_is_list:
                for n_elem in range(len(metadata_list)):
                    try:
                        if type(metadata_list[n_elem][dict_key]) is not list:
                            metadata_list[n_elem][dict_key] = [
                                metadata_list[n_elem][dict_key]
                            ]

                        just_string = False
                        this_string = ""
                        for elem in metadata_list[n_elem][dict_key]:
                            separated_elements = elem.split(": ")
                            if len(separated_elements) >= 2:

                                # if first element is larger than 40 then treat it like simple string
                                if len(separated_elements[0]) > 40:
                                    just_string = True
                                    if this_string != "":
                                        this_string = ", ".join([this_string, elem])
                                    else:
                                        this_string = elem
                                # additional elem for all bed files
                                elif len(separated_elements[0].split("(")) > 1:
                                    just_string = True
                                    if this_string != "":
                                        this_string = "(".join([this_string, elem])
                                    else:
                                        this_string = elem
                                else:
                                    list_of_elem = [
                                        separated_elements[0],
                                        ": ".join(separated_elements[1:]),
                                    ]
                                    sample_char = dict([list_of_elem])
                                    metadata_list[n_elem].update(sample_char)
                            else:
                                just_string = True
                                if this_string != "":
                                    this_string = ", ".join([this_string, elem])
                                else:
                                    this_string = elem

                        if just_string:
                            metadata_list[n_elem][dict_key] = this_string
                        else:
                            del metadata_list[n_elem][dict_key]
                    except KeyError as err:
                        self._LOGGER.warning(f"expand_metadata_list: Key Error: {err}, continuing ...")

                return metadata_list
            else:
                self._LOGGER.debug(
                    f"Metadata with {dict_key} was not expanded, as item is not list"
                )
                return metadata_list
        except KeyError as err:
            self._LOGGER.warning(f"expand_metadata_list: Key Error: {err}")
            return metadata_list
        except ValueError as err:
            self._LOGGER.warning("expand_metadata_list: Value Error: {err}")
            return metadata_list

    def _filter_gsm(self, meta_processed_samples: list, gsm_list: dict) -> list:
        """
        Getting metadata list of all samples of one experiment and filtering it
        by the list of GSM that was specified in the input files.
        And then changing names of the sample names.

        :param meta_processed_samples: list of metadata dicts of samples
        :param gsm_list: list of dicts where GSM (samples) are keys and
            sample names are values. Where values can be empty string
        """

        if gsm_list.keys():
            new_gsm_list = []
            for gsm_sample in meta_processed_samples:
                if gsm_sample["Sample_geo_accession"] in gsm_list.keys():
                    gsm_sample_new = gsm_sample
                    if gsm_list[gsm_sample["Sample_geo_accession"]] != "":
                        gsm_sample_new["sample_name"] = gsm_list[
                            gsm_sample["Sample_geo_accession"]
                        ]
                    new_gsm_list.append(gsm_sample_new)
            return new_gsm_list
        return meta_processed_samples

    @staticmethod
    def _get_list_of_keys(list_of_dict):
        """
        Getting list of all keys that are in the dictionaries in the list

        :param list list_of_dict: list of dicts with metadata
        :return list: list of dictionary keys
        """

        list_of_keys = []
        for element in list_of_dict:
            list_of_keys.extend(list(element.keys()))
        return list(set(list_of_keys))

    def _unify_list_keys(self, processed_meta_list):
        """
        Unifying list of dicts with metadata, so every dict will have
            same keys

        :param list processed_meta_list: list of dicts with metadata
        :return str: list of unified dicts with metadata
        """
        list_of_keys = self._get_list_of_keys(processed_meta_list)
        for k in list_of_keys:
            for list_elem in range(len(processed_meta_list)):
                if k not in processed_meta_list[list_elem]:
                    processed_meta_list[list_elem][k] = ""
        return processed_meta_list

    def _find_genome(self, metadata_list):
        """
        Create new genome table by joining few columns
        """
        list_keys = self._get_list_of_keys(metadata_list)
        genome_keys = [
            "assembly",
            "genome_build",
        ]
        proj_gen_keys = list(set(list_keys).intersection(genome_keys))

        for sample in enumerate(metadata_list):
            sample_genome = ""
            for key in proj_gen_keys:
                sample_genome = " ".join([sample_genome, sample[1][key]])
            metadata_list[sample[0]][NEW_GENOME_COL_NAME] = sample_genome
        return metadata_list

    def _write_gsm_annotation(
        self, gsm_metadata, file_annotation
    ):
        """
        Write metadata sheet out as an annotation file.

        :param Mapping gsm_metadata: the data to write, parsed from a file
            with metadata/annotation information
        :param str file_annotation: the path to the file to write
        :return str: path to file written
        """
        keys = list(list(gsm_metadata.values())[0].keys())

        self._LOGGER.info(f"Sample annotation sheet: {file_annotation} . Saving....")
        fp = expandpath(file_annotation)
        with open(fp, "w") as of:
            w = csv.DictWriter(of, keys, extrasaction="ignore")
            w.writeheader()
            for item in gsm_metadata:
                w.writerow(gsm_metadata[item])
        self._LOGGER.info("\033[92mFile has been saved successfully\033[0m")
        return fp

    def _write_processed_annotation(
        self,
        processed_metadata: list,
        file_annotation_path: str,
        just_object: bool = False,
    ) -> Union[NoReturn, peppy.Project]:
        """
        Saving annotation file by providing list of dictionaries with files metadata
        :param list processed_metadata: list of dictionaries with files metadata
        :param str file_annotation_path: the path to the metadata file that has to be saved
        :type just_object: True, if you want to get peppy object without saving file
        :return:
        """
        if len(processed_metadata) == 0:
            self._LOGGER.info(
                "No files found. No data to save. File %s won't be created"
                % file_annotation_path
            )
            return False

        # create folder if it does not exist
        pep_file_folder = os.path.split(file_annotation_path)[0]
        if not os.path.exists(pep_file_folder):
            os.makedirs(pep_file_folder)

        self._LOGGER.info("Unifying and saving of metadata... ")
        processed_metadata = self._unify_list_keys(processed_metadata)

        # delete rare keys
        processed_metadata = self._find_genome(processed_metadata)

        # filtering huge annotation strings that are repeating for each sample
        processed_metadata, proj_meta = self._separate_common_meta(
            processed_metadata,
            self.const_limit_project,
            self.const_limit_discard,
            self.attr_limit_truncate,
        )

        template = self._create_config_processed(file_annotation_path, proj_meta)

        if not just_object:
            with open(file_annotation_path, "w") as m_file:
                dict_writer = csv.DictWriter(m_file, processed_metadata[0].keys())
                dict_writer.writeheader()
                dict_writer.writerows(processed_metadata)
            self._LOGGER.info(
                "\033[92mFile %s has been saved successfully\033[0m"
                % file_annotation_path
            )

            # save .yaml file
            yaml_name = os.path.split(file_annotation_path)[1][:-4] + ".yaml"
            config = os.path.join(pep_file_folder, yaml_name)
            self._write(config, template, msg_pre="  Config file: ")

            # save .pep.yaml file
            if self.add_dotfile:
                dot_yaml_path = os.path.join(pep_file_folder, ".pep.yaml")
                self._create_dot_yaml(dot_yaml_path, yaml_name)

            return None

        else:
            pd_value = pd.DataFrame(processed_metadata)

            conf = yaml.load(template, Loader=yaml.Loader)
            proj = peppy.Project().from_pandas(pd_value, config=conf)
            return proj

    def _write_raw_annotation_new(self, name, metadata_dict: dict, subannot_dict: dict = None) -> Union[None, peppy.Project]:
        """
        Combining individual accessions into project-level annotations, and writing
        individual accession files (if requested)
        :param name:
        :param metadata_dict:
        :param subannot_dict:
        :return: none or peppy object
        """
        try:
            assert len(metadata_dict) > 0
        except AssertionError:
            self._LOGGER.warning(
                "\033[33mNo PEP created, as no raw data was found!!!\033[0m"
            )
            return None

        if self.discard_soft:
            clean_soft_files(os.path.join(self.metadata_root_full))

        self._LOGGER.info(
            "Creating complete project annotation sheets and config file..."
        )

        proj_root = os.path.join(self.metadata_root_full, name)
        if not os.path.exists(proj_root):
            os.makedirs(proj_root)

        proj_root_sample = os.path.join(proj_root, f"{name}{FILE_RAW_NAME_SAMPLE_PATTERN}")
        proj_root_subsample = os.path.join(proj_root, f"{name}{FILE_RAW_NAME_SUBSAMPLE_PATTERN}")
        yaml_name = f"{name}.yaml"
        proj_root_yaml = os.path.join(proj_root, yaml_name)
        dot_yaml_path = os.path.join(proj_root, ".pep.yaml")

        metadata_dict = self._check_sample_name_standard(metadata_dict)

        metadata_dict, proj_meta = self._separate_common_meta(
            metadata_dict,
            self.const_limit_project,
            self.const_limit_discard,
            self.attr_limit_truncate,
        )

        # Write combined subannotation table
        if len(subannot_dict) > 0:
            subanot_path_yaml = (
                f"subsample_table: {os.path.basename(proj_root_subsample)}"
            )
        else:
            subanot_path_yaml = f""

        template = self._create_config_raw(proj_meta, proj_root_sample, subanot_path_yaml)

        if not self.just_object:
            self._write_gsm_annotation(metadata_dict, proj_root_sample)

            if len(subannot_dict) > 0:
                self._write_subannotation(subannot_dict, proj_root_subsample)

            self._write(proj_root_yaml, template, msg_pre="  Config file: ")

            if self.add_dotfile:
                self._create_dot_yaml(dot_yaml_path, yaml_name)

        else:
            meta_df = pd.DataFrame.from_dict(metadata_dict, orient="index")

            # open list:
            new_sub_list = []
            for sub_key in subannot_dict.keys():
                new_sub_list.extend(
                    [col_item for col_item in subannot_dict[sub_key]]
                )

            sub_meta_df = pd.DataFrame(
                new_sub_list, columns=["sample_name", "SRX", "SRR"]
            )

            if sub_meta_df.empty:
                sub_meta_df = None
            else:
                sub_meta_df = [sub_meta_df]
            conf = yaml.load(template, Loader=yaml.Loader)

            proj = peppy.Project().from_pandas(meta_df, sub_meta_df, conf)
            return proj

    def _create_config_processed(self, file_annotation_path, proj_meta):
        geofetchdir = os.path.dirname(__file__)
        config_template = os.path.join(geofetchdir, CONFIG_PROCESSED_TEMPLATE_NAME)
        with open(config_template, "r") as template_file:
            template = template_file.read()
        meta_list_str = [
            f"{list(i.keys())[0]}: {list(i.values())[0]}" for i in proj_meta
        ]
        modifiers_str = "\n    ".join(d for d in meta_list_str)
        template_values = {
            "project_name": self.project_name,
            "sample_table": os.path.basename(file_annotation_path),
            "geo_folder": self.geo_folder,
            "pipeline_samples": self.file_pipeline_samples,
            "pipeline_project": self.file_pipeline_project,
            "additional_columns": modifiers_str,
        }
        for k, v in template_values.items():
            placeholder = "{" + str(k) + "}"
            template = template.replace(placeholder, str(v))
        return template

    def _create_config_raw(self, proj_meta, proj_root_sample, subanot_path_yaml):
        meta_list_str = [
            f"{list(i.keys())[0]}: {list(i.values())[0]}" for i in proj_meta
        ]
        modifiers_str = "\n    ".join(d for d in meta_list_str)
        # Write project config file
        if not self.config_template:
            geofetchdir = os.path.dirname(__file__)
            self.config_template = os.path.join(geofetchdir, CONFIG_RAW_TEMPLATE_NAME)
        with open(self.config_template, "r") as template_file:
            template = template_file.read()
        template_values = {
            "project_name": self.project_name,
            "annotation": os.path.basename(proj_root_sample),
            "subannotation": subanot_path_yaml,
            "pipeline_samples": self.file_pipeline_samples,
            "pipeline_project": self.file_pipeline_project,
            "additional_columns": modifiers_str,
        }
        for k, v in template_values.items():
            placeholder = "{" + str(k) + "}"
            template = template.replace(placeholder, str(v))
        return template

    def _check_sample_name_standard(self, metadata_dict):
        fixed_dict = {}
        for key_sample, value_sample in metadata_dict.items():
            fixed_dict[key_sample] = value_sample
            if (
                    value_sample["sample_name"] == ""
                    or value_sample["sample_name"] is None
            ):
                fixed_dict[key_sample]["sample_name"] = value_sample["Sample_title"]
            # sanitize names
            fixed_dict[key_sample]["sample_name"] = self._sanitize_name(
                fixed_dict[key_sample]["sample_name"]
            )
        metadata_dict = fixed_dict
        metadata_dict = self._standardize_colnames(metadata_dict)
        return metadata_dict

    @staticmethod
    def _sanitize_name(name_str: str):
        """
        Function that sanitizing strings. (Replace all odd characters)
        :param str name_str: Any string value that has to be sanitized.
        :return: sanitized strings
        """
        new_str = name_str
        punctuation1 = r"""!"#$%&'()*,./:;<=>?@[\]^_`{|}~"""
        for odd_char in list(punctuation1):
            new_str = new_str.replace(odd_char, "_")
        new_str = new_str.replace(" ", "_").replace("__", "_")
        return new_str

    @staticmethod
    def _create_dot_yaml(file_path: str, yaml_path: str):
        """
        Function that creates .pep.yaml file that points to actual yaml file
        :param str file_path: Path to the .pep.yaml file that we want to create
        :param str yaml_path: path or name of the actual yaml file
        """
        with open(file_path, "w+") as file:
            file.writelines(f"config_file: {yaml_path}")

    def _separate_common_meta(
        self,
        meta_list: Union[List, Dict],
        max_len: int = 50,
        del_limit: int = 250,
        attr_limit_truncate: int = 500,
    ):
        """
        This function is separating information for the experiment from a sample
        :param list or dict meta_list: list of dictionaries of samples
        :param int max_len: threshold of the length of the common value that can be stored in the sample table
        :param int del_limit: threshold of the length of the common value that have to be deleted
        :param int attr_limit_truncate: max length of the attribute in the sample csv
        :return set: Return is a set of list, where 1 list (or dict) is
        list of samples metadata dictionaries and 2: list of common samples metadata
        dictionaries that are linked to the project.
        """
        # check if meta_list is dict and converting it to list
        input_is_dict = False
        if isinstance(meta_list, dict):
            input_is_dict = True
            meta_list = self._dict_to_list_convector(proj_dict=meta_list)

        list_of_keys = self._get_list_of_keys(meta_list)
        list_keys_diff = []
        # finding columns with common values
        for this_key in list_of_keys:
            value = ""
            for nb_sample in enumerate(meta_list):
                try:
                    if nb_sample[0] == 0:
                        value = meta_list[nb_sample[0]][this_key]
                        if len(str(value)) < max_len and len(str(value)) < del_limit:
                            list_keys_diff.append(this_key)
                            break
                    else:
                        if value != meta_list[nb_sample[0]][this_key]:
                            list_keys_diff.append(this_key)
                            break
                except KeyError:
                    pass

        list_keys_diff = set(list_keys_diff)

        # separating sample and common metadata and creating 2 lists
        new_meta_project = []
        for this_key in list_of_keys:
            first_key = True
            for nb_sample in enumerate(meta_list):
                try:
                    if this_key not in list_keys_diff:
                        if first_key:
                            if len(str(nb_sample[1][this_key])) <= del_limit:
                                new_str = nb_sample[1][this_key]
                                if isinstance(nb_sample[1][this_key], str):
                                    new_str = nb_sample[1][this_key].replace('"', "")
                                    new_str = re.sub("[^A-Za-z0-9]+", " ", new_str)
                                new_meta_project.append({this_key: new_str})
                            first_key = False
                        del meta_list[nb_sample[0]][this_key]
                except KeyError:
                    pass

        # Truncate huge information in metadata
        new_list = []
        for this_item in meta_list:
            new_item_list = {}
            for key, value in this_item.items():
                if len(str(value)) < attr_limit_truncate:
                    new_item_list[key] = value
                else:
                    new_item_list[key] = value[0:attr_limit_truncate] + " ..."
            new_list.append(new_item_list)

        meta_list = new_list

        if input_is_dict:
            meta_list = self._dict_to_list_convector(proj_list=meta_list)
        return meta_list, new_meta_project

    def _standardize_colnames(self, meta_list: Union[list, dict]):
        """
        Standardize column names by lower-casing and underscore
        :param list meta_list: list of dictionaries of samples
        :return : list of dictionaries of samples with standard colnames
        """
        # check if meta_list is dict and converting it to list
        input_is_dict = False
        if isinstance(meta_list, dict):
            input_is_dict = True
            meta_list = self._dict_to_list_convector(proj_dict=meta_list)

        new_metalist = []
        list_keys = self._get_list_of_keys(meta_list)
        for item_nb, values in enumerate(meta_list):
            new_metalist.append({})
            for key in list_keys:
                try:
                    new_key_name = key.lower().strip()
                    new_key_name = self._sanitize_name(new_key_name)

                    new_metalist[item_nb][new_key_name] = values[key]

                except KeyError:
                    pass

        if input_is_dict:
            new_metalist = self._dict_to_list_convector(proj_list=new_metalist)

        return new_metalist

    @staticmethod
    def _dict_to_list_convector(
        proj_dict: Dict = None, proj_list: List = None
    ) -> Union[Dict, List]:
        """
        Convector project dict to list and vice versa
        :param proj_dict: project dictionary
        :param proj_list: project list
        :return: converted values
        """
        if proj_dict is not None:
            new_meta_list = []
            for key in proj_dict:
                new_dict = proj_dict[key]
                new_dict["big_key"] = key
                new_meta_list.append(new_dict)

            meta_list = new_meta_list

        elif proj_list is not None:
            new_sample_dict = {}
            for sample in proj_list:
                new_sample_dict[sample["big_key"]] = sample
            meta_list = new_sample_dict

        else:
            raise ValueError

        return meta_list

    def _download_SRA_file(self, run_name):
        """
        Downloading SRA file by ising 'prefetch' utility from the SRA Toolkit
        more info: (http://www.ncbi.nlm.nih.gov/books/NBK242621/)
        :param str run_name: SRR number of the SRA file
        """

        # Set up a simple loop to try a few times in case of failure
        t = 0
        while True:
            t = t + 1
            subprocess_return = run_subprocess(
                ["prefetch", run_name, "--max-size", "50000000"]
            )

            if subprocess_return == 0:
                break

            if t >= NUM_RETRIES:
                raise RuntimeError(
                    f"Prefetch retries of {run_name} failed. Try this sample later"
                )

            self._LOGGER.info(
                "Prefetch attempt failed, wait a few seconds to try again"
            )
            time.sleep(t * 2)

    @staticmethod
    def _which(program):
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

    def _sra_bam_conversion(self, bam_file, run_name):
        """
        Converting of SRA file to BAM file by using samtools function "sam-dump"
        :param str bam_file: path to BAM file that has to be created
        :param str run_name: SRR number of the SRA file that has to be converted
        """
        self._LOGGER.info("Converting to bam: " + run_name)
        sra_file = os.path.join(self.sra_folder, run_name + ".sra")
        if not os.path.exists(sra_file):
            raise FileNotFoundError(sra_file)

        # The -u here allows unaligned reads, and seems to be
        # required for some sra files regardless of aligned state
        cmd = (
            "sam-dump -u "
            + os.path.join(self.sra_folder, run_name + ".sra")
            + " | samtools view -bS - > "
            + bam_file
        )
        # sam-dump -u SRR020515.sra | samtools view -bS - > test.bam

        self._LOGGER.info(f"Conversion command: {cmd}")
        run_subprocess(cmd, shell=True)

    @staticmethod
    def _update_columns(metadata, experiment_name, sample_name, read_type):
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
        exp["organism"] = exp["Sample_organism_ch1"]
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

    def _sra_bam_conversion2(self, bam_file, run_name, picard_path=None):
        """
        Converting of SRA file to BAM file by using fastq-dump
        (is used when sam-dump fails, yielding an empty bam file. Here fastq -> bam conversion is used)
        :param str bam_file: path to BAM file that has to be created
        :param str run_name: SRR number of the SRA file that has to be converted
        :param str picard_path: Path to The Picard toolkit. More info: https://broadinstitute.github.io/picard/
        """

        # check to make sure it worked
        cmd = (
            "fastq-dump --split-3 -O "
            + os.path.realpath(self.sra_folder)
            + " "
            + os.path.join(self.sra_folder, run_name + ".sra")
        )
        self._LOGGER.info(f"Command: {cmd}")
        run_subprocess(cmd, shell=True)
        if not picard_path:
            self._LOGGER.warning("Can't convert the fastq to bam without picard path")
        else:
            # was it paired data? you have to process it differently
            # so it knows it's paired end
            fastq0 = os.path.join(self.sra_folder, run_name + ".fastq")
            fastq1 = os.path.join(self.sra_folder, run_name + "_1.fastq")
            fastq2 = os.path.join(self.sra_folder, run_name + "_2.fastq")

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
            run_subprocess(cmd, shell=True)

    def _write_subannotation(self, tabular_data, filepath, column_names=None):
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
        with open(fp, "w") as openfile:
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

    def _download_file(self, file_url, data_folder, new_name=None, sleep_after=0.5):
        """
        Given an url for a file, downloading to specified folder
        :param str file_url: the URL of the file to download
        :param str data_folder: path to the folder where data should be downloaded
        :param float sleep_after: time to sleep after downloading
        :param str new_name: new file name in the
        """
        filename = os.path.basename(file_url)
        if new_name is None:
            full_filepath = os.path.join(data_folder, filename)
        else:
            full_filepath = os.path.join(data_folder, new_name)

        if not os.path.exists(full_filepath):
            self._LOGGER.info(f"\033[38;5;242m")  # set color to gray
            # if dir does not exist:
            if not os.path.exists(data_folder):
                os.makedirs(data_folder)
            ret = run_subprocess(
                ["wget", "--no-clobber", file_url, "-O", full_filepath]
            )
            self._LOGGER.info(f"\033[38;5;242m{ret}\033[0m")
            time.sleep(sleep_after)
            self._LOGGER.info(f"\033[0m")  # Reset to default terminal color
        else:
            self._LOGGER.info(f"\033[38;5;242mFile {full_filepath} exists.\033[0m")

    def _get_list_of_processed_files(
        self, file_gse_content: list, file_gsm_content: list
    ):
        """
        Given a paths to GSE and GSM metafile create a list of dicts of metadata of processed files
        :param list file_gse_content: list of lines of gse metafile
        :param list file_gsm_content: list of lines of gse metafile
        :return list: list of metadata of processed files
        """
        tar_re = re.compile(r".*\.tar$")
        gse_numb = None
        meta_processed_samples = []
        meta_processed_series = {"GSE": "", "files": []}
        for line in file_gse_content:

            if re.compile(r"!Series_geo_accession").search(line):
                gse_numb = self._get_value(line)
                meta_processed_series["GSE"] = gse_numb
            found = re.findall(SER_SUPP_FILE_PATTERN, line)

            if found:
                pl = parse_SOFT_line(line)
                file_url = pl[list(pl.keys())[0]].rstrip()
                filename = os.path.basename(file_url)
                self._LOGGER.debug(f"Processed GSE file found: %s" % str(file_url))

                # search for tar file:
                if tar_re.search(filename):
                    # find and download filelist - file with information about files in tar
                    index = file_url.rfind("/")
                    tar_files_list_url = (
                        "https" + file_url[3 : index + 1] + "filelist.txt"
                    )
                    # file_list_name
                    filelist_path = os.path.join(
                        self.metadata_expanded, gse_numb + "_file_list.txt"
                    )

                    # TODO: make new function of code below:
                    if not os.path.isfile(filelist_path) or self.refresh_metadata:
                        result = requests.get(tar_files_list_url)
                        if result.ok:
                            result.encoding = "UTF-8"
                            filelist_raw_text = result.text
                            if not self.discard_soft:
                                try:
                                    with open(filelist_path, "w") as f:
                                        f.write(filelist_raw_text)
                                except OSError:
                                    self._LOGGER.warning(
                                        f"{filelist_path} not found. File won't be saved.."
                                    )

                        else:
                            raise Exception(f"error in requesting tar_files_list")
                    else:
                        self._LOGGER.info(f"Found previous GSM file: {filelist_path}")
                        filelist_obj = open(filelist_path, "r")
                        filelist_raw_text = filelist_obj.read()

                    nb = len(meta_processed_samples) - 1
                    for line_gsm in file_gsm_content:
                        if line_gsm[0] == "^":
                            nb = len(self._check_file_existance(meta_processed_samples))
                            meta_processed_samples.append(
                                {"files": [], "GSE": gse_numb}
                            )
                        else:
                            try:
                                pl = parse_SOFT_line(line_gsm.strip("\n"))
                            except IndexError:
                                continue
                            element_keys = list(pl.keys())[0]
                            element_values = list(pl.values())[0]
                            if not re.findall(SUPP_FILE_PATTERN, line_gsm):
                                if (
                                    element_keys
                                    not in meta_processed_samples[nb].keys()
                                ):
                                    meta_processed_samples[nb].update(pl)
                                else:
                                    if (
                                        type(meta_processed_samples[nb][element_keys])
                                        is not list
                                    ):
                                        meta_processed_samples[nb][element_keys] = [
                                            meta_processed_samples[nb][element_keys]
                                        ]
                                        meta_processed_samples[nb][element_keys].append(
                                            element_values
                                        )
                                    else:
                                        meta_processed_samples[nb][element_keys].append(
                                            element_values
                                        )

                        found_gsm = re.findall(SUPP_FILE_PATTERN, line_gsm)

                        if found_gsm:
                            pl = parse_SOFT_line(line_gsm)
                            file_url_gsm = pl[list(pl.keys())[0]].rstrip()
                            self._LOGGER.debug(
                                f"Processed GSM file found: %s" % str(file_url_gsm)
                            )
                            if file_url_gsm != "NONE":
                                meta_processed_samples[nb]["files"].append(file_url_gsm)

                    self._check_file_existance(meta_processed_samples)
                    meta_processed_samples = self._separate_list_of_files(
                        meta_processed_samples
                    )
                    meta_processed_samples = self._separate_file_url(
                        meta_processed_samples
                    )

                    self._LOGGER.info(
                        f"\nTotal number of processed SAMPLES files found is: "
                        f"%s" % str(len(meta_processed_samples))
                    )

                    # expand meta_processed_samples with information about type and size
                    file_info_add = self._read_tar_filelist(filelist_raw_text)
                    for index_nr in range(len(meta_processed_samples)):
                        file_name = meta_processed_samples[index_nr]["file"]
                        meta_processed_samples[index_nr].update(
                            file_info_add[file_name]
                        )

                    if self.filter_re:
                        meta_processed_samples = self._run_filter(
                            meta_processed_samples
                        )
                    if self.filter_size:
                        meta_processed_samples = self._run_size_filter(
                            meta_processed_samples
                        )

                # other files than .tar: saving them into meta_processed_series list
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
                self._LOGGER.debug(
                    f"IndexError in adding value to meta_processed_series: %s" % ind_err
                )

        meta_processed_series = self._separate_list_of_files(meta_processed_series)
        meta_processed_series = self._separate_file_url(meta_processed_series)
        self._LOGGER.info(
            f"Total number of processed SERIES files found is: "
            f"%s" % str(len(meta_processed_series))
        )
        if self.filter_re:
            meta_processed_series = self._run_filter(meta_processed_series)

        return meta_processed_samples, meta_processed_series

    @staticmethod
    def _check_file_existance(meta_processed_sample):
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
    def _separate_list_of_files(meta_list, col_name="files"):
        """
        This method is separating list of files (dict value) or just simple dict
        into two different dicts
        """
        separated_list = []
        if isinstance(meta_list, list):
            for meta_elem in meta_list:
                for file_elem in meta_elem[col_name]:
                    new_dict = meta_elem.copy()
                    new_dict.pop(col_name, None)
                    new_dict["file"] = file_elem
                    separated_list.append(new_dict)
        elif isinstance(meta_list, dict):
            for file_elem in meta_list[col_name]:
                new_dict = meta_list.copy()
                new_dict.pop(col_name, None)
                new_dict["file"] = file_elem
                separated_list.append(new_dict)
        else:
            return TypeError("Incorrect type")

        return separated_list

    def _separate_file_url(self, meta_list):
        """
        This method is adding dict key without file_name without path
        """
        separated_list = []
        for meta_elem in meta_list:
            new_dict = meta_elem.copy()
            new_dict["file_url"] = meta_elem["file"]
            new_dict["file"] = os.path.basename(meta_elem["file"])
            # new_dict["sample_name"] = os.path.basename(meta_elem["file"])
            try:
                new_dict["sample_name"] = str(meta_elem["Sample_title"])
                if new_dict["sample_name"] == "" or new_dict["sample_name"] is None:
                    raise KeyError("sample_name Does not exist. Creating .. ")
            except KeyError:
                new_dict["sample_name"] = os.path.basename(meta_elem["file"])

            # sanitize sample names
            new_dict["sample_name"] = self._sanitize_name(new_dict["sample_name"])

            separated_list.append(new_dict)
        return separated_list

    def _run_filter(self, meta_list, col_name="file"):
        """
        If user specified filter it will filter all this files here by col_name
        """
        filtered_list = []
        for meta_elem in meta_list:
            if self.filter_re.search(meta_elem[col_name].lower()):
                filtered_list.append(meta_elem)
        self._LOGGER.info(
            "\033[32mTotal number of files after filter is: %i \033[0m"
            % len(filtered_list)
        )

        return filtered_list

    def _run_size_filter(self, meta_list, col_name="file_size"):
        """
        function for filtering file size
        """
        if self.filter_size is not None:
            filtered_list = []
            for meta_elem in meta_list:
                if int(meta_elem[col_name]) <= self.filter_size:
                    filtered_list.append(meta_elem)
        else:
            self._LOGGER.info(
                "\033[32mTotal number of files after size filter NONE?? \033[0m"
            )
            return meta_list
        self._LOGGER.info(
            "\033[32mTotal number of files after size filter is: %i \033[0m"
            % len(filtered_list)
        )
        return filtered_list

    @staticmethod
    def _read_tar_filelist(raw_text: str):
        """
        Creating list for supplementary files that are listed in "filelist.txt"
        :param str raw_text: path to the file with information about files that are zipped ("filelist.txt")
        :return dict: dict of supplementary file names and additional information
        """
        f = StringIO(raw_text)
        files_info = {}
        csv_reader = csv.reader(f, delimiter="\t")
        line_count = 0
        for row in csv_reader:
            if line_count == 0:
                name_index = row.index("Name")
                size_index = row.index("Size")
                type_index = row.index("Type")

                line_count += 1
            else:
                files_info[row[name_index]] = {
                    "file_size": row[size_index],
                    "type": row[type_index],
                }

        return files_info

    @staticmethod
    def _get_value(all_line: str):
        """
        :param all_line: string with key value. (e.g. '!Series_geo_accession = GSE188720')
        :return: value (e.g. GSE188720)
        """
        line_value = all_line.split("= ")[-1]
        return line_value.split(": ")[-1].rstrip("\n")

    def _download_processed_file(self, file_url, data_folder):
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

        if not self.geo_folder:
            self._LOGGER.error(
                "You must provide a geo_folder to download processed data."
            )
            sys.exit(1)

        filename = os.path.basename(file_url)
        ntry = 0

        while ntry < 10:
            try:
                self._download_file(file_url, data_folder)
                self._LOGGER.info(
                    "\033[92mFile %s has been downloaded successfully\033[0m"
                    % f"{data_folder}/{filename}"
                )
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

    def _get_SRA_meta(self, file_gse_content: list, gsm_metadata, file_sra=None):
        """
        Parse out the SRA project identifier from the GSE file
        :param list file_gse_content: list of content of file_sde_content
        :param dict gsm_metadata: dict of GSM metadata
        :param str file_sra: full path to SRA.csv metafile that has to be downloaded
        """
        #
        acc_SRP = None
        for line in file_gse_content:
            found = re.findall(PROJECT_PATTERN, line)
            if found:
                acc_SRP = found[0]
                self._LOGGER.info(f"Found SRA Project accession: {acc_SRP}")
                break

        if not acc_SRP:
            # If I can't get an SRA accession, maybe raw data wasn't submitted to SRA
            # as part of this GEO submission. Can't proceed.
            self._LOGGER.warning(
                "\033[91mUnable to get SRA accession (SRP#) from GEO GSE SOFT file. "
                "No raw data detected! Continuing anyway...\033[0m"
            )
            # but wait; another possibility: there's no SRP linked to the GSE, but there
            # could still be an SRX linked to the (each) GSM.
            if len(gsm_metadata) == 1:
                try:
                    acc_SRP = gsm_metadata.keys()[0]
                    self._LOGGER.warning(
                        "But the GSM has an SRX number; instead of an "
                        "SRP, using SRX identifier for this sample: " + acc_SRP
                    )
                except TypeError:
                    self._LOGGER.warning("Error in gsm_metadata")
                    return False

            # else:
            #     # More than one sample? not sure what to do here. Does this even happen?
            #     continue
        # Now we have an SRA number, grab the SraRunInfo Metadata sheet:
        # The SRARunInfo sheet has additional sample metadata, which we will combine
        # with the GSM file to produce a single sample a
        if file_sra is not None:
            if not os.path.isfile(file_sra) or self.refresh_metadata:
                try:
                    # downloading metadata
                    srp_list = self._get_SRP_list(acc_SRP)
                    srp_list = self._unify_list_keys(srp_list)
                    if file_sra is not None and not self.discard_soft:
                        with open(file_sra, "w") as m_file:
                            dict_writer = csv.DictWriter(m_file, srp_list[0].keys())
                            dict_writer.writeheader()
                            dict_writer.writerows(srp_list)

                    return srp_list

                except Exception as err:
                    self._LOGGER.warning(
                        f"\033[91mError occurred, while downloading SRA Info Metadata of {acc_SRP}. "
                        f"Error: {err}  \033[0m"
                    )
                    return False
            else:
                # open existing annotation
                self._LOGGER.info(f"Found SRA metadata, opening..")
                with open(file_sra, "r") as m_file:
                    reader = csv.reader(m_file)
                    file_list = []
                    srp_list = []
                    for k in reader:
                        file_list.append(k)
                    for value_list in file_list[1:]:
                        srp_list.append(dict(zip(file_list[0], value_list)))

                    return srp_list
        else:
            try:
                srp_list = self._get_SRP_list(acc_SRP)
                return srp_list

            except Exception as err:
                self._LOGGER.warning(
                    f"\033[91mError occurred, while downloading SRA Info Metadata of {acc_SRP}. "
                    f"Error: {err}  \033[0m"
                )
                return False

    def _get_SRP_list(self, srp_number: str) -> list:
        """
        By using requests and xml searching and getting list of dicts of SRRs
        :param str srp_number: SRP number
        :return: list of dicts of SRRs
        """
        if not srp_number:
            self._LOGGER.info(f"No srp number in this accession found")
            return []
        self._LOGGER.info(f"Downloading {srp_number} sra metadata")
        ncbi_esearch = NCBI_ESEARCH.format(SRP_NUMBER=srp_number)

        # searching ids responding to srp
        x = requests.post(ncbi_esearch)

        if x.status_code != 200:
            x.encoding = "UTF-8"
            self._LOGGER.error(f"Error in ncbi esearch response: {x.status_code}")
            raise x.raise_for_status()
        id_results = x.json()["esearchresult"]["idlist"]
        if len(id_results) > 500:
            id_results = [
                id_results[x : x + 100] for x in range(0, len(id_results), 100)
            ]
        else:
            id_results = [id_results]

        SRP_list = []
        for result in id_results:
            id_r_string = ",".join(result)
            id_api = NCBI_EFETCH.format(ID=id_r_string)

            y = requests.get(id_api)
            if y.status_code != 200:
                self._LOGGER.error(
                    f"Error in ncbi efetch response in SRA fetching: {x.status_code}"
                )
                raise y.raise_for_status()
            xml_result = y.text
            SRP_list.extend(xmltodict.parse(xml_result)["SraRunInfo"]["Row"])

        return SRP_list

    def _read_gsm_metadata(
        self, acc_GSE: str, acc_GSE_list: dict, file_gsm_content: list
    ) -> dict:
        """
        A simple state machine to parse SOFT formatted files (Here, the GSM file)

        :param str acc_GSE: GSE number (Series accession)
        :param dict acc_GSE_list: list of GSE
        :param list file_gsm_content: list of contents of gsm file
        :return dict: dictionary of experiment information (gsm_metadata)
        """
        gsm_metadata = {}

        # Get GSM#s (away from sample_name)
        GSM_limit_list = list(acc_GSE_list[acc_GSE].keys())

        # save the state
        current_sample_id = None
        current_sample_srx = False
        samples_list = []
        for line in file_gsm_content:
            line = line.rstrip()
            if len(line) == 0:  # Apparently SOFT files can contain blank lines
                continue
            if line[0] == "^":
                pl = parse_SOFT_line(line)
                if (
                    len(acc_GSE_list[acc_GSE]) > 0
                    and pl["SAMPLE"] not in GSM_limit_list
                ):
                    # sys.stdout.write("  Skipping " + a['SAMPLE'] + ".")
                    current_sample_id = None
                    continue
                current_sample_id = pl["SAMPLE"]
                current_sample_srx = False
                gsm_metadata[current_sample_id] = {
                    "sample_name": "",
                    "protocol": "",
                    "organism": "",
                    "read_type": "",
                    "data_source": None,
                    "SRR": None,
                    "SRX": None,
                }

                self._LOGGER.debug(f"Found sample: {current_sample_id}")
                samples_list.append(current_sample_id)
            elif current_sample_id is not None:
                try:
                    pl = parse_SOFT_line(line)
                except IndexError:
                    self._LOGGER.debug(
                        f"Failed to parse alleged SOFT line for sample ID {current_sample_id}; "
                        f"line: {line}"
                    )
                    continue
                new_key = list(pl.keys())[0]
                if new_key in gsm_metadata[current_sample_id]:
                    if isinstance(gsm_metadata[current_sample_id][new_key], list):
                        gsm_metadata[current_sample_id][new_key].append(pl[new_key])
                    else:
                        gsm_metadata[current_sample_id][new_key] = [
                            gsm_metadata[current_sample_id][new_key]
                        ]
                        gsm_metadata[current_sample_id][new_key].append(pl[new_key])
                else:
                    gsm_metadata[current_sample_id].update(pl)

                # Now convert the ids GEO accessions into SRX accessions
                if not current_sample_srx:
                    found = re.findall(EXPERIMENT_PATTERN, line)
                    if found:
                        self._LOGGER.debug(f"(SRX accession: {found[0]})")
                        srx_id = found[0]
                        gsm_metadata[srx_id] = gsm_metadata.pop(current_sample_id)
                        gsm_metadata[srx_id][
                            "gsm_id"
                        ] = current_sample_id  # save the GSM id
                        current_sample_id = srx_id
                        current_sample_srx = True
        # GSM SOFT file parsed, save it in a list
        self._LOGGER.info(f"Processed {len(samples_list)} samples.")
        gsm_metadata = self._expand_metadata_list_in_dict(gsm_metadata)
        return gsm_metadata

    def _write(self, f_var_value, content, msg_pre=None, omit_newline=False):
        fp = expandpath(f_var_value)
        self._LOGGER.info((msg_pre or "") + fp)
        with open(fp, "w") as f:
            f.write(content)
            if not omit_newline:
                f.write("\n")


def _parse_cmdl(cmdl):
    parser = argparse.ArgumentParser(
        description="Automatic GEO and SRA data downloader"
    )

    processed_group = parser.add_argument_group("processed")
    raw_group = parser.add_argument_group("raw")

    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # Required
    parser.add_argument(
        "-i",
        "--input",
        dest="input",
        required=True,
        help="required: a GEO (GSE) accession, or a file with a list of GSE numbers",
    )

    # Optional
    parser.add_argument(
        "-n", "--name", help="Specify a project name. Defaults to GSE number"
    )

    parser.add_argument(
        "-m",
        "--metadata-root",
        dest="metadata_root",
        default=safe_echo("SRAMETA"),
        help="Specify a parent folder location to store metadata. "
        "The project name will be added as a subfolder "
        "[Default: $SRAMETA:" + safe_echo("SRAMETA") + "]",
    )

    parser.add_argument(
        "-u",
        "--metadata-folder",
        help="Specify an absolute folder location to store metadata. "
        "No subfolder will be added. Overrides value of --metadata-root "
        "[Default: Not used (--metadata-root is used by default)]",
    )

    parser.add_argument(
        "--just-metadata",
        action="store_true",
        help="If set, don't actually run downloads, just create metadata",
    )

    parser.add_argument(
        "-r",
        "--refresh-metadata",
        action="store_true",
        help="If set, re-download metadata even if it exists.",
    )

    parser.add_argument(
        "--config-template", default=None, help="Project config yaml file template."
    )

    # Optional
    parser.add_argument(
        "--pipeline-samples",
        default=None,
        help="Optional: Specify one or more filepaths to SAMPLES pipeline interface yaml files. "
        "These will be added to the project config file to make it immediately "
        "compatible with looper. [Default: null]",
    )

    # Optional
    parser.add_argument(
        "--pipeline-project",
        default=None,
        help="Optional: Specify one or more filepaths to PROJECT pipeline interface yaml files. "
        "These will be added to the project config file to make it immediately "
        "compatible with looper. [Default: null]",
    )
    # Optional
    parser.add_argument(
        "--disable-progressbar",
        action="store_true",
        help="Optional: Disable progressbar",
    )

    # Optional
    parser.add_argument(
        "-k",
        "--skip",
        default=0,
        type=int,
        help="Skip some accessions. [Default: no skip].",
    )

    parser.add_argument(
        "--acc-anno",
        action="store_true",
        help="Optional: Produce annotation sheets for each accession."
        " Project combined PEP for the whole project won't be produced.",
    )

    parser.add_argument(
        "--discard-soft",
        action="store_true",
        help="Optional: After creation of PEP files, all soft and additional files will be deleted",
    )

    parser.add_argument(
        "--const-limit-project",
        type=int,
        default=50,
        help="Optional: Limit of the number of the constant sample characters "
        "that should not be in project yaml. [Default: 50]",
    )

    parser.add_argument(
        "--const-limit-discard",
        type=int,
        default=250,
        help="Optional: Limit of the number of the constant sample characters "
        "that should not be discarded [Default: 250]",
    )

    parser.add_argument(
        "--attr-limit-truncate",
        type=int,
        default=500,
        help="Optional: Limit of the number of sample characters."
        "Any attribute with more than X characters will truncate to the first X,"
        " where X is a number of characters [Default: 500]",
    )

    parser.add_argument(
        "--add-dotfile",
        action="store_true",
        help="Optional: Add .pep.yaml file that points .yaml PEP file",
    )

    processed_group.add_argument(
        "-p",
        "--processed",
        default=False,
        action="store_true",
        help="Download processed data [Default: download raw data].",
    )

    processed_group.add_argument(
        "--data-source",
        dest="data_source",
        choices=["all", "samples", "series"],
        default="samples",
        help="Optional: Specifies the source of data on the GEO record"
        " to retrieve processed data, which may be attached to the"
        " collective series entity, or to individual samples. "
        "Allowable values are: samples, series or both (all). "
        "Ignored unless 'processed' flag is set. [Default: samples]",
    )

    processed_group.add_argument(
        "--filter",
        default=None,
        help="Optional: Filter regex for processed filenames [Default: None]."
        "Ignored unless 'processed' flag is set.",
    )

    processed_group.add_argument(
        "--filter-size",
        dest="filter_size",
        default=None,
        help="""Optional: Filter size for processed files
                that are stored as sample repository [Default: None].
                Works only for sample data.
                Supported input formats : 12B, 12KB, 12MB, 12GB. 
                Ignored unless 'processed' flag is set.""",
    )

    processed_group.add_argument(
        "-g",
        "--geo-folder",
        default=safe_echo("GEODATA"),
        help="Optional: Specify a location to store processed GEO files."
        " Ignored unless 'processed' flag is set."
        "[Default: $GEODATA:" + safe_echo("GEODATA") + "]",
    )

    raw_group.add_argument(
        "-x",
        "--split-experiments",
        action="store_true",
        help="""Split SRR runs into individual samples. By default, SRX
            experiments with multiple SRR Runs will have a single entry in the
            annotation table, with each run as a separate row in the
            subannotation table. This setting instead treats each run as a
            separate sample""",
    )

    raw_group.add_argument(
        "-b",
        "--bam-folder",
        dest="bam_folder",
        default=safe_echo("SRABAM"),
        help="""Optional: Specify folder of bam files. Geofetch will not
            download sra files when corresponding bam files already exist.
            [Default: $SRABAM:"""
        + safe_echo("SRABAM")
        + "]",
    )

    raw_group.add_argument(
        "-f",
        "--fq-folder",
        dest="fq_folder",
        default=safe_echo("SRAFQ"),
        help="""Optional: Specify folder of fastq files. Geofetch will not
            download sra files when corresponding fastq files already exist.
            [Default: $SRAFQ:"""
        + safe_echo("SRAFQ")
        + "]",
    )

    # Deprecated; these are for bam conversion which now happens in sra_convert
    # it still works here but I hide it so people don't use it, because it's confusing.
    raw_group.add_argument(
        "-s",
        "--sra-folder",
        dest="sra_folder",
        default=safe_echo("SRARAW"),
        help=argparse.SUPPRESS,
        # help="Optional: Specify a location to store sra files "
        #   "[Default: $SRARAW:" + safe_echo("SRARAW") + "]"
    )
    raw_group.add_argument(
        "--bam-conversion",
        action="store_true",
        # help="Turn on sequential bam conversion. Default: No conversion.",
        help=argparse.SUPPRESS,
    )

    raw_group.add_argument(
        "--picard-path",
        dest="picard_path",
        default=safe_echo("PICARD"),
        # help="Specify a path to the picard jar, if you want to convert "
        # "fastq to bam [Default: $PICARD:" + safe_echo("PICARD") + "]",
        help=argparse.SUPPRESS,
    )

    raw_group.add_argument(
        "--use-key-subset",
        action="store_true",
        help="Use just the keys defined in this module when writing out metadata.",
    )

    logmuse.add_logging_options(parser)
    return parser.parse_args(cmdl)


def safe_echo(var):
    """Returns an environment variable if it exists, or an empty string if not"""
    return os.getenv(var, "")


class InvalidSoftLineException(Exception):
    """Exception related to parsing SOFT line."""

    def __init__(self, l):
        """
        Create the exception by providing the problematic line.

        :param str l: the problematic SOFT line
        """
        super(self, f"{l}")


def main():
    """Run the script."""
    args = _parse_cmdl(sys.argv[1:])
    args_dict = vars(args)
    args_dict["args"] = args
    Geofetcher(**args_dict).fetch_all(args_dict["input"])



if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        print("Pipeline aborted.")
        sys.exit(1)
