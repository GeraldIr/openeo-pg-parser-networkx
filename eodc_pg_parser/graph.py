from __future__ import annotations

import functools
import logging
import random
from collections import namedtuple
from dataclasses import dataclass, field
from functools import partial
from typing import Callable, Dict, List, Optional, Set
from uuid import UUID

import networkx as nx

from eodc_pg_parser.pg_schema import (
    ParameterReference,
    PGEdgeType,
    ProcessGraph,
    ProcessNode,
    ResultReference,
)
from eodc_pg_parser.utils import ProcessGraphUnflattener, parse_nested_parameter

logger = logging.getLogger(__name__)

ArgSubstitution = namedtuple("ArgSubstitution", ["arg_name", "access_func", "key"])


@dataclass
class EvalEnv:
    """
    Object to pass which parameter references are available for each node throughout walking the graph.
    """

    parent: Optional[EvalEnv]
    node: ProcessNode
    node_name: str
    process_graph_uid: str
    result: bool = False
    parameters: Set[str] = field(default_factory=set)
    result_references_to_walk: List[EvalEnv] = field(default_factory=list)
    callbacks_to_walk: Dict[str, ProcessGraph] = field(default_factory=dict)

    def search_for_parameter_env(self, arg_name: str) -> EvalEnv:
        """
        Recursively search for a parameter in a node's lineage. The most specific parameter (i.e. from the closest ancestor) is used.
        """
        if arg_name in self.parameters:
            return self
        if self.parent:
            return self.parent.search_for_parameter_env(arg_name)
        raise ProcessParameterMissing(
            f"ProcessParameter {arg_name} missing for node {self.node_uid}."
        )

    # This decorator makes this property not recompute each time it's called.
    @functools.cached_property
    def node_uid(self):
        return f"{self.node_name}-{self.process_graph_uid}"

    def __hash__(self) -> int:
        return hash(self.node_uid)

    def __repr__(self):
        return f"""\n
        ---------------------------------------
        EVAL_ENV {self.node_uid}
        parameters: {self.parameters}
        parent: {self.parent}
        ---------------------------------------
        """


UNRESOLVED_CALLBACK_VALUE = "__UNRESOLVED_CALLBACK__"
UNRESOLVED_RESULT_REFERENCE_VALUE = "__UNRESOLVED_RESULT_REFERENCE__"


class ProcessParameterMissing(Exception):
    pass


class OpenEOProcessGraph(object):
    def __init__(self, pg_data: Dict):
        self.G = nx.DiGraph()

        nested_raw_graph = self._unflatten_raw_process_graph(pg_data)
        self.nested_graph = self._parse_datamodel(nested_raw_graph)

        # Start parsing the graph at the result node of the top-level graph.
        self._EVAL_ENV = None

        self._parse_process_graph(self.nested_graph)

    @staticmethod
    def _unflatten_raw_process_graph(raw_flat_graph: Dict) -> Dict:
        """
        Translates a flat process graph into a nested structure by resolving the from_node references.
        """
        if "process_graph" not in raw_flat_graph:
            raw_flat_graph = {"process_graph": raw_flat_graph}

        nested_graph = {
            "process_graph": {
                "root": ProcessGraphUnflattener.unflatten(raw_flat_graph["process_graph"])
            }
        }
        logger.warning("Deserialised process graph into nested structure")
        return nested_graph

    @staticmethod
    def _parse_datamodel(nested_graph: Dict) -> ProcessGraph:
        """
        Parses a nested process graph into the Pydantic datamodel for ProcessGraph.
        """

        return ProcessGraph.parse_obj(nested_graph)

    def _parse_process_graph(self, process_graph: ProcessGraph, arg_name: str = None):
        """
        Start recursively walking a process graph from its result node and parse its information into self.G.
        This step passes process_graph.uid to make sure that each process graph operates within its own namespace so that nodes are unique.
        """

        for node_name, node in process_graph.process_graph.items():
            if node.result:
                self._EVAL_ENV = EvalEnv(
                    parent=self._EVAL_ENV,
                    node=node,
                    node_name=node_name,
                    process_graph_uid=process_graph.uid,
                    result=True,
                )
                if self._EVAL_ENV.parent:
                    self.G.add_edge(
                        self._EVAL_ENV.parent.node_uid,
                        self._EVAL_ENV.node_uid,
                        reference_type=PGEdgeType.Callback,
                        arg_name=arg_name,
                    )
                self._walk_node()
                self._EVAL_ENV = self._EVAL_ENV.parent
                return
        raise Exception("Process graph has no return node!")

    def _parse_argument(
        self,
        arg: ProcessArgument,
        arg_name: str,
        access_func: Callable,
        real_origin_node: str = None,
    ):

        if isinstance(arg, ParameterReference):
            # Search parent nodes for the referenced parameter.
            # self._resolve_parameter_reference(
            #     parameter_reference=arg, arg_name=arg_name, access_func=access_func
            # )
            pass

        elif isinstance(arg, ResultReference):
            # Only add a subnode for walking if it's in the same process grpah, otherwise you get infinite loops!
            from_node_eval_env = EvalEnv(
                parent=self._EVAL_ENV.parent,
                node=arg.node,
                node_name=arg.from_node,
                process_graph_uid=self._EVAL_ENV.process_graph_uid,
            )

            target_node = (
                real_origin_node if real_origin_node else from_node_eval_env.node_uid
            )

            self.G.add_edge(
                self._EVAL_ENV.node_uid,
                target_node,
                reference_type=PGEdgeType.ResultReference,
            )

            if (
                "arg_substitutions"
                not in self.G.edges[self._EVAL_ENV.node_uid, target_node]
            ):
                self.G.edges[self._EVAL_ENV.node_uid, target_node][
                    "arg_substitutions"
                ] = []

            self.G.edges[self._EVAL_ENV.node_uid, target_node][
                "arg_substitutions"
            ].append(
                ArgSubstitution(arg_name=arg_name, access_func=access_func, key=arg_name)
            )

            if (
                from_node_eval_env.process_graph_uid == self._EVAL_ENV.process_graph_uid
                and not real_origin_node
            ):
                self._EVAL_ENV.result_references_to_walk.append(from_node_eval_env)

            access_func(new_value=arg, set_bool=True)
            self._EVAL_ENV.parameters.add(arg_name)

        # dicts and list parameters can contain further result or parameter references, so have to parse these exhaustively.
        elif isinstance(arg, dict):
            access_func(new_value={}, set_bool=True)

            for k, v in arg.items():
                access_func()[k] = None

                parsed_arg = parse_nested_parameter(v)

                sub_access_func = partial(
                    lambda key, access_func, new_value=None, set_bool=False: access_func()[
                        key
                    ]
                    if not set_bool
                    else access_func().__setitem__(key, new_value),
                    key=k,
                    access_func=access_func,
                )
                self._parse_argument(parsed_arg, arg_name, access_func=sub_access_func)

        elif isinstance(arg, list):
            access_func(new_value=[], set_bool=True)

            for i, element in enumerate(arg):
                access_func().append(None)
                parsed_arg = parse_nested_parameter(element)

                sub_access_func = partial(
                    lambda key, access_func, new_value=None, set_bool=False: access_func()[
                        key
                    ]
                    if not set_bool
                    else access_func().__setitem__(key, new_value),
                    key=i,
                    access_func=access_func,
                )
                self._parse_argument(parsed_arg, arg_name, access_func=sub_access_func)

        elif isinstance(arg, ProcessGraph):
            self._EVAL_ENV.callbacks_to_walk[arg_name] = arg

        else:
            access_func(new_value=arg, set_bool=True)
            self._EVAL_ENV.parameters.add(arg_name)

    def _walk_node(self):
        """
        Parse all the required information from the current node into self.G and recursively walk child nodes.
        """
        print(f"Walking node {self._EVAL_ENV.node_uid}")

        self.G.add_node(
            self._EVAL_ENV.node_uid,
            process_id=self._EVAL_ENV.node.process_id,
            resolved_kwargs={},
            node_name=self._EVAL_ENV.node_name,
            process_graph_uid=self._EVAL_ENV.process_graph_uid,
            result=self._EVAL_ENV.result,
        )

        for arg_name, unpacked_arg in self._EVAL_ENV.node.arguments.items():

            # Put the raw arg into the resolved_kwargs dict. If there are no further references within, that's already the right kwarg to pass on.
            # If there are further references, doing this will ensure that the container for these references is already there
            # and the access_functions can inject the resolved parameters later.
            self.G.nodes[self._EVAL_ENV.node_uid]["resolved_kwargs"][
                arg_name
            ] = unpacked_arg

            # This just points to the resolved_kwarg itself!
            access_func = partial(
                lambda node_uid, arg_name, new_value=None, set_bool=False: self.G.nodes[
                    node_uid
                ]["resolved_kwargs"][arg_name]
                if not set_bool
                else self.G.nodes[node_uid]["resolved_kwargs"].__setitem__(
                    arg_name, new_value
                ),
                node_uid=self._EVAL_ENV.node_uid,
                arg_name=arg_name,
            )
            self._parse_argument(unpacked_arg, arg_name, access_func=access_func)

        for arg_name, arg in self._EVAL_ENV.callbacks_to_walk.items():
            self.G.nodes[self._EVAL_ENV.node_uid]["resolved_kwargs"][
                arg_name
            ] = UNRESOLVED_CALLBACK_VALUE
            self._parse_process_graph(arg, arg_name=arg_name)

        for sub_eval_env in self._EVAL_ENV.result_references_to_walk:
            self._EVAL_ENV = sub_eval_env
            self._walk_node()

    def __iter__(self) -> str:
        """
        Traverse the process graph to yield nodes in the order they need to be executed.
        """
        top_level_graph = self._get_sub_graph(self.uid)
        visited_nodes = set()
        unlocked_nodes = [
            node for node, out_degree in top_level_graph.out_degree() if out_degree == 0
        ]
        while unlocked_nodes:
            node = unlocked_nodes.pop()
            visited_nodes.add(node)
            for child_node, _ in top_level_graph.in_edges(node):
                ready = True
                for _, uncle_node in top_level_graph.out_edges(child_node):
                    if uncle_node not in visited_nodes:
                        ready = False
                        break
                if ready and child_node not in visited_nodes:
                    unlocked_nodes.append(child_node)
            yield node

    def _get_sub_graph(self, process_graph_id: str) -> nx.DiGraph:
        return self.G.subgraph(
            [
                node_id
                for node_id, data in self.G.nodes(data=True)
                if data["process_graph_uid"] == process_graph_id
            ]
        )

    @property
    def nodes(self) -> List:
        return list(self.G.nodes(data=True))

    @property
    def edges(self) -> List:
        return list(self.G.edges(data=True))

    @property
    def in_edges(self, node: str) -> List:
        return list(self.G.in_edges(node, data=True))

    @property
    def uid(self) -> UUID:
        return self.nested_graph.uid

    @property
    def result_node(self) -> str:
        return [
            node
            for node, in_degree in self.G.in_degree()
            if in_degree == 0
            if self.G.nodes(data=True)[node]["result"]
        ][0]

    def plot(self, reverse=False):
        if reverse:
            self.G = self.G.reverse()

        if self.G.number_of_nodes() < 1:
            logger.warning("Graph has no nodes, nothing to plot.")
            return

        sub_graphs = {
            process_graph_uid
            for _, process_graph_uid in nx.get_node_attributes(
                self.G, "process_graph_uid"
            ).items()
        }

        random.seed(42)
        node_colour_palette = {
            sub_graph_uid: random.randint(0, 255) for sub_graph_uid in sub_graphs
        }
        edge_colour_palette = {
            PGEdgeType.ResultReference: "blue",
            PGEdgeType.Callback: "red",
        }
        node_colours = [
            node_colour_palette[self.G.nodes(data=True)[node]["process_graph_uid"]]
            for node in self.G.nodes
        ]
        edge_colors = [
            edge_colour_palette.get(self.G.edges[edge]["reference_type"], "green")
            for edge in self.G.edges
        ]

        nx.draw_circular(
            self.G,
            labels=nx.get_node_attributes(self.G, "node_name"),
            horizontalalignment="right",
            verticalalignment="top",
            node_color=node_colours,
            edge_color=edge_colors,
        )

        if reverse:
            self.G = self.G.reverse()
