#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

from wireviz.metadata import Metadata
from wireviz.notes import get_page_notes
from wireviz.page_options import get_page_options
from wireviz.parse_yaml import parse_concat_merge_files
from wireviz.wv_dataclasses import AUTOGENERATED_PREFIX
from wireviz.wv_harness import Harness
from wireviz.wv_utils import (
    expand,
    get_single_key_and_value,
    smart_file_resolve,
)


def parse(
    inp: List[Path],
    metadata_files: List[Path],
    return_types: Union[None, str, Tuple[str]] = None,
    output_formats: Union[None, str, Tuple[str]] = None,
    output_dir: Path = None,
    extra_metadata: Dict = {},
    shared_bom: Dict = {},
) -> Any:
    """
    This function takes an input, parses it as a WireViz Harness file,
    and outputs the result as one or more files and/or as a function return value

    Accepted inputs:
        * A List of Path object pointing to a YAML source file to parse

    Supported return types:
        * "png":     the diagram as raw PNG data
        * "svg":     the diagram as raw SVG data
        * "harness": the diagram as a Harness Python object

    Supported output formats:
        * "csv":  the BOM, as a comma-separated text file
        * "gv":   the diagram, as a GraphViz source file
        * "html": the diagram and (depending on the template) the BOM, as a HTML file
        * "png":  the diagram, as a PNG raster image
        * "pdf":  the diagram and (depending on the template) the BOM, as a PDF file
        * "svg":  the diagram, as a SVG vector image
        * "tsv":  the BOM, as a tab-separated text file

    Args:
        inp:
            The input to be parsed (see above for accepted inputs).
        return_types (optional):
            One of the supported return types (see above), or a tuple of multiple return types.
            If set to None, no output is returned by the function.
        output_formats (optional):
            One of the supported output types (see above), or a tuple of multiple output formats.
            If set to None, no files are generated.
        output_dir (Path | str, optional):
            The directory to place the generated output files.
            Defaults to inp's parent directory, or cwd if inp is not a path.
        extra_metadata (Dict, optional):
            Any metadata to add to the template.
            Normally, this should contain programmatic metadata

    Returns:
        Depending on the return_types parameter, may return:
        * None
        * A dict of {return_type: data}
    """

    if not output_formats and not return_types:
        raise Exception("No output formats or return types specified")

    yaml_file = inp[-1]
    yaml_data = parse_concat_merge_files(inp, metadata_files)

    image_paths = {f.parent for f in inp if f.parent.is_dir()}

    output_dir = yaml_file.parent if not output_dir else output_dir
    output_name = yaml_file.stem

    # define variables =========================================================
    # containers for parsed component data and connection sets
    template_connectors = {}
    template_cables = {}
    connection_sets = []
    # actual harness
    try:
        metadata = Metadata(
            **{
                **{
                    "output_name": output_name,
                    "title": yaml_file.stem,
                },
                **yaml_data.get("metadata", {}),
                **extra_metadata,
            }
        )
    except TypeError:
        logging.error(
            "Metadata definition is missing an argument, refer to trace for which one\n\tsee src/wirevize/metdata.py for a definition of the metadata fields"
        )
        raise

    harness = Harness(
        metadata=metadata,
        options=get_page_options(yaml_data, output_name),
        notes=get_page_notes(yaml_data, output_name),
        shared_bom=shared_bom,
    )
    # others
    # store mapping of components to their respective template
    designators_and_templates = {}
    # keep track of auto-generated designators to avoid duplicates
    autogenerated_designators = {}

    # add items
    # parse YAML input file ====================================================

    sections = ["connectors", "cables", "connections"]
    types = [dict, dict, list]
    for sec, ty in zip(sections, types):
        if sec in yaml_data and type(yaml_data[sec]) == ty:  # section exists
            if len(yaml_data[sec]) > 0:  # section has contents
                if ty == dict:
                    for key, attribs in yaml_data[sec].items():
                        # The Image dataclass might need to open
                        # an image file with a relative path.
                        image = attribs.get("image")
                        if isinstance(image, dict):
                            image_path = Path(image["src"])
                            if image_path and not image_path.is_absolute():
                                # resolve relative image path
                                image["src"] = smart_file_resolve(
                                    image_path, image_paths
                                )
                        if sec == "connectors":
                            template_connectors[key] = attribs
                        elif sec == "cables":
                            template_cables[key] = attribs
            else:  # section exists but is empty
                pass
        else:  # section does not exist, create empty section
            if ty == dict:
                yaml_data[sec] = {}
            elif ty == list:
                yaml_data[sec] = []

    connection_sets = yaml_data["connections"]

    # go through connection sets, generate and connect components ==============

    template_separator_char = harness.options.template_separator

    def resolve_designator(inp, separator):
        if separator in inp:  # generate a new instance of an item
            if inp.count(separator) > 1:
                raise Exception(f"{inp} - Found more than one separator ({separator})")
            template, designator = inp.split(separator)
            if designator == "":
                autogenerated_designators[template] = (
                    autogenerated_designators.get(template, 0) + 1
                )
                designator = (
                    f"{AUTOGENERATED_PREFIX}"
                    f"{template}_{autogenerated_designators[template]}"
                )
            # check if redefining existing component to different template
            if designator in designators_and_templates:
                if designators_and_templates[designator] != template:
                    raise Exception(
                        f"Trying to redefine {designator}"
                        f" from {designators_and_templates[designator]} to {template}"
                    )
            else:
                designators_and_templates[designator] = template
        else:
            template, designator = (inp, inp)
            if designator in designators_and_templates:
                pass  # referencing an exiting connector, no need to add again
            else:
                designators_and_templates[designator] = template
        return (template, designator)

    # utilities to check for alternating connectors and cables ==========

    alternating_types = ["connector", "cable"]
    expected_type = None

    def check_type(designator, template, actual_type):
        nonlocal expected_type
        if not expected_type:  # each connection set may start with either section
            expected_type = actual_type

        if actual_type != expected_type:  # did not alternate
            raise Exception(
                f'Expected {expected_type}, but "{designator}" ("{template}") is {actual_type}'
            )

    def alternate_type():  # flip between connector and cable
        nonlocal expected_type
        expected_type = alternating_types[1 - alternating_types.index(expected_type)]

    for connection_set in connection_sets:

        # figure out number of parallel connections within this set
        connectioncount = []
        for entry in connection_set:
            if isinstance(entry, list):
                connectioncount.append(len(entry))
            elif isinstance(entry, dict):
                connectioncount.append(len(expand(list(entry.values())[0])))
                # e.g.: - X1: [1-4,6] yields 5
            else:
                pass  # strings do not reveal connectioncount

        if not any(connectioncount):
            raise ValueError("No connection count found in connection set")

        # check that all entries are the same length
        if len(set(connectioncount)) > 1:
            raise Exception(
                "All items in connection set must reference the same number of connections"
            )
        # all entries are the same length, connection count is set
        connectioncount = connectioncount[0]

        # expand string entries to list entries of correct length
        for index, entry in enumerate(connection_set):
            if isinstance(entry, str):
                connection_set[index] = [entry] * connectioncount

        # resolve all designators
        for index, entry in enumerate(connection_set):
            if isinstance(entry, list):
                for subindex, item in enumerate(entry):
                    template, designator = resolve_designator(
                        item, template_separator_char
                    )
                    connection_set[index][subindex] = designator
            elif isinstance(entry, dict):
                key = list(entry.keys())[0]
                template, designator = resolve_designator(key, template_separator_char)
                value = entry[key]
                connection_set[index] = {designator: value}
            else:
                pass  # string entries have been expanded in previous step

        # expand all pin lists
        for index, entry in enumerate(connection_set):
            if isinstance(entry, list):
                connection_set[index] = [{designator: 1} for designator in entry]
            elif isinstance(entry, dict):
                designator = list(entry.keys())[0]
                pinlist = expand(entry[designator])
                connection_set[index] = [{designator: pin} for pin in pinlist]
            else:
                pass  # string entries have been expanded in previous step

        # Populate wiring harness ==============================================

        expected_type = None  # reset check for alternating types
        # at the beginning of every connection set
        # since each set may begin with either type

        # generate components
        for entry in connection_set:
            for item in entry:
                designator = list(item.keys())[0]
                template = designators_and_templates[designator]

                if designator in harness.connectors:  # existing connector instance
                    check_type(designator, template, "connector")
                elif template in template_connectors.keys():
                    # generate new connector instance from template
                    check_type(designator, template, "connector")
                    harness.add_connector(
                        designator=designator, **template_connectors[template]
                    )

                elif designator in harness.cables:  # existing cable instance
                    check_type(designator, template, "cable")
                elif template in template_cables.keys():
                    # generate new cable instance from template
                    check_type(designator, template, "cable")
                    harness.add_cable(
                        designator=designator, **template_cables[template]
                    )
                else:
                    raise Exception(
                        f"{template} is an unknown template/designator"
                    )

            # entries in connection set must alternate between connectors and cables
            alternate_type()

        # transpose connection set list
        # before: one item per component, one subitem per connection in set
        # after:  one item per connection in set, one subitem per component
        connection_set = list(map(list, zip(*connection_set)))

        # connect components
        for index_entry, entry in enumerate(connection_set):
            for index_item, item in enumerate(entry):
                designator = list(item.keys())[0]

                if designator in harness.cables:
                    if index_item == 0:
                        # list started with a cable, no connector to join on left side
                        from_name, from_pin = (None, None)
                    else:
                        from_name, from_pin = get_single_key_and_value(
                            entry[index_item - 1]
                        )
                    via_name, via_pin = (designator, item[designator])
                    if index_item == len(entry) - 1:
                        # list ends with a cable, no connector to join on right side
                        to_name, to_pin = (None, None)
                    else:
                        to_name, to_pin = get_single_key_and_value(
                            entry[index_item + 1]
                        )
                    harness.connect(
                        from_name, from_pin, via_name, via_pin, to_name, to_pin
                    )

    if "additional_bom_items" in yaml_data:
        for line in yaml_data["additional_bom_items"]:
            try:
                harness.add_additional_bom_item(line)
            except TypeError as e:
                logging.error(f"Failed to add line {line} as an additional bom item")
                raise

    # harness population completed =============================================

    harness.populate_bom()

    if output_formats:
        harness.output(
            filename=output_dir / output_name, fmt=output_formats, view=False
        )

    if return_types:
        if isinstance(return_types, str):  # only one return type speficied
            return_types = [return_types]

        return_types = [t.lower() for t in return_types]

        returns = {}
        for rt in return_types:
            if rt == "png":
                returns["png"] = harness.png
            if rt == "svg":
                returns["svg"] = harness.svg
            if rt == "harness":
                returns["harness"] = harness
            if rt == "shared_bom":
                returns["shared_bom"] = harness.shared_bom

        return returns
