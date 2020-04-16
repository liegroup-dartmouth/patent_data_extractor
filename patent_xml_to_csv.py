#!/usr/bin/env python3

""" docdb_to_csv.py """

import argparse
import csv
import json
import logging
import re
from collections import defaultdict
from io import BytesIO
from pathlib import Path

from lxml import etree

try:
    from termcolor import colored
except ImportError:
    logging.debug("termcolor (pip install termcolor) not available")

    def colored(text, _color):
        """ Dummy function in case termcolor is not available. """
        return text


def replace_missing_mathml_ents(doc):
    """ Substitute out some undefined entities that appear in the XML -- see notes
        for further details. """
    doc = doc.replace("&IndentingNewLine;", "&#xF3A3;")
    doc = doc.replace("&LeftBracketingBar;", "&#xF603;")
    doc = doc.replace("&RightBracketingBar;", "&#xF604;")
    doc = doc.replace("&LeftDoubleBracketingBar;", "&#xF605;")
    doc = doc.replace("&RightDoubleBracketingBar;", "&#xF606;")
    return doc


class DTDResolver(etree.Resolver):
    def __init__(self, dtd_path):
        self.dtd_path = Path(dtd_path)

    def resolve(self, system_url, _public_id, context):
        if system_url.startswith(str(self.dtd_path)):
            return self.resolve_filename(system_url, context)
        else:
            return self.resolve_filename(
                str((self.dtd_path / system_url).resolve()), context,
            )


class DocdbToTabular:
    def __init__(
        self,
        xml_input,
        config,
        dtd_path,
        recurse,
        output_path,
        no_validate,
        logger,
        **_kwargs,
    ):

        self.logger = logger

        self.xml_files = Path(xml_input)
        if self.xml_files.is_file():
            self.xml_files = [self.xml_files]
        elif self.xml_files.is_dir():
            self.xml_files = self.xml_files.glob(f'{"**/" if recurse else ""}*.xml')
        else:
            self.logger.fatal("specified input is invalid")
            exit(1)

        # do this now, because we don't want to process all that data and then find
        #  the output_path is invalid... :)
        self.output_path = Path(output_path)
        self.output_path.mkdir(parents=True, exist_ok=True)

        self.config = json.load(open(config))

        self.tables = defaultdict(list)

        if no_validate:
            self.parser = etree.XMLParser(
                load_dtd=True, resolve_entities=True, ns_clean=True
            )
        else:
            self.parser = etree.XMLParser(
                load_dtd=True, resolve_entities=True, ns_clean=True, dtd_validation=True
            )
        self.parser.resolvers.add(DTDResolver(dtd_path))

    @staticmethod
    def get_all_xml_docs(filepath):
        with open(filepath, "r") as _fh:
            data = _fh.read()
        return re.split(r"\n(?=<\?xml)", data)

    @staticmethod
    def yield_xml_doc(filepath):
        xml_doc = []
        with open(filepath, "r") as _fh:
            for line in _fh:
                if line.startswith("<?xml"):
                    if xml_doc:
                        yield "".join(xml_doc)
                    xml_doc = []
                xml_doc.append(line)

    @staticmethod
    def get_text(elem):
        return re.sub(
            r"\s+", " ", etree.tostring(elem, method="text", encoding="unicode")
        ).strip()

    def get_pk(self, tree, config):
        if config.get("pk", None):
            elems = tree.findall("./" + config["pk"])
            assert len(elems) == 1
            return self.get_text(elems[0])
        return None

    def process_path(
        self, tree, path, config, record, parent_entity=None, parent_pk=None
    ):

        try:
            elems = [tree.getroot()]
        except AttributeError:
            elems = tree.findall("./" + path)

        if isinstance(config, str):
            if elems:

                if config.startswith("|"):
                    record[config[1:]] = "|".join(
                        [self.get_text(elem) for elem in elems]
                    )
                    return

                try:
                    assert len(elems) == 1
                except AssertionError:
                    self.logger.fatal("Multiple elements found for %s", path)
                    self.logger.fatal([self.get_text(el) for el in elems])
                    raise

                # handle enum types
                if ":" in config:
                    record[config.split(":")[0]] = config.split(":")[1]
                    return
                record[config] = self.get_text(elems[0])
            return

        # config is not a simple fieldname to write to, so we've got an entity type
        entity = config["entity"]
        for elem in elems:
            srecord = {}

            pk = self.get_pk(tree, config)
            if pk:
                srecord["id"] = pk
            else:
                srecord["id"] = f"{len(self.tables[entity])}"

            if parent_pk:
                srecord[f"{parent_entity}_id"] = parent_pk
            for subpath, subconfig in config["fields"].items():
                self.process_path(elem, subpath, subconfig, srecord, entity, pk)

            self.tables[entity].append(srecord)

    def process_doc(self, doc):

        doc = replace_missing_mathml_ents(doc)

        tree = etree.parse(BytesIO(doc.encode("utf8")), self.parser)

        for path, config in self.config.items():
            self.process_path(tree, path, config, {})

    def convert(self):
        for input_file in self.xml_files:

            self.logger.info(colored("Processing %s...", "green"), input_file.resolve())

            for i, doc in enumerate(self.yield_xml_doc(input_file)):
                if i % 100 == 0:
                    self.logger.debug(
                        colored("Processing document %d...", "cyan"), i + 1
                    )
                try:
                    self.process_doc(doc)
                except (AssertionError, etree.XMLSyntaxError) as exc:
                    self.logger.debug(doc)
                    p_id = re.search(
                        r"<B210><DNUM><PDAT>(\d+)<\/PDAT><\/DNUM><\/B210>", doc
                    ).group(1)
                    self.logger.warning(
                        colored("ID %s: %s (record has not been parsed)", "red"),
                        p_id,
                        exc.msg,
                    )

            self.logger.info(colored("...%d records processed!", "green"), i + 1)

    def get_fieldnames(self):
        """ On python >=3.7, dictionaries maintain key order, so fields are guaranteed to be
            returned in the order in which they appear in the config file.  To guarantee this
            on versions of python <3.7 (insofar as it matters), collections.OrderedDict would
            have to be used here.
        """

        fieldnames = defaultdict(list)

        def add_fieldnames(config, _fieldnames, parent_entity=None):
            if isinstance(config, str):
                if ":" in config:
                    _fieldnames.append(config.split(":")[0])
                    return
                if config.startswith("|"):
                    _fieldnames.append(config[1:])
                    return
                _fieldnames.append(config)
                return

            entity = config["entity"]
            _fieldnames = []
            if config.get("pk") or parent_entity:
                _fieldnames.append("id")
            if parent_entity:
                _fieldnames.append(f"{parent_entity}_id")
            for subconfig in config["fields"].values():
                add_fieldnames(subconfig, _fieldnames, entity)
            # different keys may be appending rows to the same table(s), so we're appending
            #  to lists of fieldnames here.
            fieldnames[entity] = list(
                dict.fromkeys(fieldnames[entity] + _fieldnames).keys()
            )

        for config in self.config.values():
            add_fieldnames(config, [])

        return fieldnames

    def write_csv_files(self):

        fieldnames = self.get_fieldnames()

        self.logger.info(
            colored("Writing csv files to %s ...", "green"), self.output_path.resolve()
        )
        for tablename, rows in self.tables.items():
            output_file = self.output_path / f"{tablename}.csv"
            with output_file.open("w") as _fh:
                writer = csv.DictWriter(_fh, fieldnames=fieldnames[tablename])
                writer.writeheader()
                writer.writerows(rows)


def main():
    """ Command-line entry-point. """
    arg_parser = argparse.ArgumentParser(description="Description: {}".format(__file__))

    arg_parser.add_argument(
        "-v", "--verbose", action="store_true", default=False, help="increase verbosity"
    )
    arg_parser.add_argument(
        "-q", "--quiet", action="store_true", default=False, help="quiet operation"
    )

    arg_parser.add_argument(
        "-i",
        "--xml-input",
        action="store",
        required=True,
        help='"XML" file or directory to parse recursively',
    )

    arg_parser.add_argument(
        "-r",
        "--recurse",
        action="store_true",
        help='if supplied, the parser will search subdirectories for "XML" files to parse',
    )

    arg_parser.add_argument(
        "-c",
        "--config",
        action="store",
        required=True,
        help="config file (in JSON format) describing the fields to extract from the XML",
    )

    arg_parser.add_argument(
        "-d",
        "--dtd-path",
        action="store",
        required=True,
        help="path to folder where dtds and related documents can be found",
    )

    arg_parser.add_argument(
        "-o",
        "--output-path",
        action="store",
        required=True,
        help="path to folder in which to save output (will be created if necessary)",
    )

    arg_parser.add_argument(
        "--no-validate",
        action="store_true",
        help="skip validation of input XML (for speed)",
    )

    args = arg_parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_level = logging.CRITICAL if args.quiet else log_level
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)  # format="%(message)s")
    logger.addHandler(logging.StreamHandler())

    convertor = DocdbToTabular(**vars(args), logger=logger)
    convertor.convert()
    convertor.write_csv_files()


if __name__ == "__main__":
    main()
