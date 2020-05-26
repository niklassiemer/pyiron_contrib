# coding: utf-8
# Copyright (c) Max-Planck-Institut für Eisenforschung GmbH - Computational Materials Design (CM) Department
# Distributed under the terms of "New BSD License", see the LICENSE file.

from __future__ import print_function

from pyiron.base.generic.hdfio import ProjectHDFio
from pyiron_contrib.utils.misc import LoggerMixin
from pyiron_contrib.protocol.io import Input, Output
from abc import ABC, abstractmethod
from pyiron_contrib.protocol.utils.event import Event, EventHandler
from pyiron_contrib.utils.hdf import generic_to_hdf, open_if_group, generic_from_hdf
from pyiron_contrib.protocol.data_types import resolve_if_lazy
from pyiron.base.generic.hdfio import FileHDFio
import os

"""
The goal here is to abstract and simplify the graph functionality.
"""


__author__ = "Liam Huber, Dominik Gehringer"
__copyright__ = "Copyright 2019, Max-Planck-Institut für Eisenforschung GmbH " \
                "- Computational Materials Design (CM) Department"
__version__ = "0.0"
__maintainer__ = "Liam Huber"
__email__ = "huber@mpie.de"
__status__ = "development"
__date__ = "Feb 10, 2020"


class Vertex(LoggerMixin, ABC):
    DEFAULT_STATE = "next"
    DEFAULT_WHITELIST = {}

    def __init__(self, *args, vertex_name=None, **kwargs):
        super(Vertex, self).__init__()

        self.vertex_name = vertex_name
        self._vertex_state = self.DEFAULT_STATE
        self.possible_vertex_states = [self.DEFAULT_STATE]
        self.parent_graph = None
        self._hdf = None

        self.input = Input()
        self.output = Output()
        self.init_io_channels()

        self.archive = Archive(owner=self, whitelist=self.DEFAULT_WHITELIST)

    @property
    def vertex_state(self):
        return self._vertex_state

    @vertex_state.setter
    def vertex_state(self, new_state):
        if new_state not in self.possible_vertex_states:
            raise ValueError("New state not in list of possible states")
        self._vertex_state = new_state

    @property
    def hdf(self):
        if self.parent_graph is not None:
            return self.parent_graph.hdf.open('vertices/' + self.vertex_name)
        elif self._hdf is None:
            hdf_location = os.path.abspath(os.getcwd())
            hdf_name = self.vertex_name or self.__class__.__name__
            self._hdf = FileHDFio(file_name=os.path.join(hdf_location, hdf_name))
            return self._hdf
        else:
            return self._hdf

    @hdf.setter
    def hdf(self, new_hdf):
        if not isinstance(new_hdf, (ProjectHDFio, FileHDFio)):
            raise TypeError("New HDFs must be of type ProjectHDFio or FileHDFio, but vertex {} got type {}".format(
                self.vertex_name, type(new_hdf)
            ))
        self._hdf = new_hdf

    @abstractmethod
    def init_io_channels(self):
        """Define channels for vertex input and output."""
        pass

    def execute(self):
        """Just parse the input and do your physics, then store the output."""
        output_data = self.function(**self.input.resolve()) or {}
        self.update_and_archive(output_data)

    @abstractmethod
    def function(self, *args, **kwargs):
        """
        The vertex-specific logic to be executed.

        Args:
            Must have one arg/kwarg variable for each input channel with the same name.

        Returns:
            (dict): With items matching output channels (or empty dict if no output).
        """
        pass

    def update_and_archive(self, output_data):
        for key, value in output_data.items():
            getattr(self.output, key).push(value)
        self.archive.update()

    def get_graph_location(self):
        return self._get_graph_location()[:-1]  # Cut the trailing underscore

    def _get_graph_location(self, loc=""):
        new_loc = self.vertex_name + "_" + loc
        if self.parent_graph is None:
            return new_loc
        else:
            return self.parent_graph._get_graph_location(loc=new_loc)

    def to_hdf(self, hdf, group_name=None):
        """
        Store the Vertex in an HDF5 file.

        Args:
            hdf (ProjectHDFio): HDF5 group object.
            group_name (str): HDF5 subgroup name. (Default is None.)
        """
        hdf5_server = open_if_group(hdf, group_name)
        hdf5_server["TYPE"] = str(type(self))
        hdf5_server["possiblevertexstates"] = self.possible_vertex_states
        hdf5_server["vertexstate"] = self.vertex_state
        hdf5_server["vertexname"] = self.vertex_name
        self.input.to_hdf(hdf=hdf5_server, group_name="input")
        self.output.to_hdf(hdf=hdf5_server, group_name="output")

    def from_hdf(self, hdf, group_name=None):
        """
        Load the Vertex from an HDF5 file.

        Args:
            hdf (ProjectHDFio): HDF5 group object.
            group_name (str): HDF5 subgroup name. (Default is None.)
        """
        hdf5_server = open_if_group(hdf, group_name)
        self.possible_vertex_states = hdf5_server["possiblevertexstates"]
        self._vertex_state = hdf5_server["vertexstate"]
        self.vertex_name = hdf5_server["vertexname"]
        self.input.from_hdf(hdf=hdf5_server, group_name="input")
        self.output.from_hdf(hdf=hdf5_server, group_name="output")

    def finish(self):
        self.archive.finish()


class Graph(Vertex):
    def __init__(self, *args, hdf=None, **kwargs):
        super(Graph, self).__init__(*args, **kwargs)

        self._hdf = hdf

        # Declare attributes
        self.vertices = Vertices(self)
        self.edges = Edges()
        self.starting_vertex = None
        self.restarting_vertex = None
        self.active_vertex = None

        # Set up the graph
        self.set_vertices()
        self._initialize_edges()
        self.set_edges()
        self.wire_data_flow()

        # Prepare active vertex
        if self.starting_vertex is None:
            self.logger.warning("Starting vertex not set for {}".format(self.vertex_name))
        if self.restarting_vertex is None:
            self.logger.warning("Restarting vertex not set for {}".format(self.vertex_name))
        self.active_vertex = self.starting_vertex

        # Initialize event system
        self.graph_finished = Event()
        self.graph_started = Event()
        self.vertex_processing = Event()
        self.vertex_processed = Event()

    @abstractmethod
    def set_vertices(self):
        """Add child vertices to the graph."""
        pass

    def _initialize_edges(self):
        for v in self.vertices.values():
            self.edges.initialize(v)

    @abstractmethod
    def set_edges(self):
        """Wire the logic for traversing the graph edges."""
        pass

    @abstractmethod
    def wire_data_flow(self):
        """Connect input and output information inside the graph. Also set the archive clock for all vertices."""
        pass

    def function(self, *args, **kwargs):
        self.graph_started.fire()

        self.subscribe_all_vertices_to_event(self.graph_finished)

        while self.active_vertex is not None:
            self.vertex_processing.fire(self.active_vertex)
            self.active_vertex.execute()
            self.vertex_processed.fire(self.active_vertex)
            self.step()
        output_data = self.get_output()
        self.graph_finished.fire()
        return output_data

    def subscribe_all_vertices_to_event(self, event):
        for vertex_name, vertex in self.vertices.items():
            handler_name = '{}_close_handler'.format(vertex_name)
            if not event.has_handler(handler_name):
                event += EventHandler(handler_name, vertex.finish)

    def step(self):
        """
        Follows the edge out of the active vertex to get the name of the next vertex and set it as the active vertex.
        If the active vertex has multiple possible states, the outbound edge for the current state will be chosen.
        """
        vertex = self.active_vertex
        if vertex is not None:
            next_vertex_name = self.edges[vertex.vertex_name][vertex.vertex_state]
            try:
                self.active_vertex = self.vertices[next_vertex_name]
            except KeyError:
                self.active_vertex = None

    @abstractmethod
    def get_output(self):
        """Collect (and possibly rename) data from child vertices and return as a dict matching output channels."""
        pass

    def reset(self):
        if self.active_vertex is not None:
            raise ValueError("Tried to restart {}, but graph was at {} instead of None".format(
                self.vertex_name, self.active_vertex.vertex_name))

        if self.restarting_vertex is None:
            self.logger.warning("Reseting graph {} but found no restarting vertex.".format(self.vertex_name))

        self.active_vertex = self.restarting_vertex

    def __getattr__(self, item):
        return getattr(self.vertices, item)

    def set_clock_for_all_vertices(self, clock):
        for v in self.vertices.values():
            v.archive.clock = clock

    def to_hdf(self, hdf, group_name=None):
        """
        Store the Vertex in an HDF5 file.

        Args:
            hdf (ProjectHDFio): HDF5 group object.
            group_name (str): HDF5 subgroup name. (Default is None.)
        """
        super(Graph, self).to_hdf(hdf, group_name=group_name)
        hdf5_server = open_if_group(hdf, group_name)
        hdf5_server["TYPE"] = str(type(self))
        hdf5_server["startingvertexname"] = self.starting_vertex.vertex_name
        hdf5_server["restartingvertexname"] = self.restarting_vertex.vertex_name
        self.vertices.to_hdf(hdf5_server, "vertices")
        self.edges.to_hdf(hdf5_server, "edges")

    def from_hdf(self, hdf, group_name=None):
        """
        Load the Protocol from an HDF5 file.

        Args:
            hdf (ProjectHDFio): HDF5 group object - optional
            group_name (str): HDF5 subgroup name - optional
        """
        super(Graph, self).from_hdf(hdf=hdf, group_name=group_name)
        hdf5_server = open_if_group(hdf, group_name)

        self.vertices.from_hdf(hdf5_server, "vertices")
        self.edges.from_hdf(hdf5_server, "edges")

        starting_vertex_name = hdf5_server["startingvertexname"]
        restarting_vertex_name = hdf5_server["restartingvertexname"]
        self.starting_vertex = self.vertices[starting_vertex_name]
        self.restarting_vertex = self.vertices[restarting_vertex_name]
        self.active_vertex = None
        self.wire_data_flow()


class DotDict(dict):
    """A dictionary which allows `.` setting and getting for items."""

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __getattr__(self, item):
        try:
            return self.__getitem__(item)
        except KeyError:
            raise AttributeError("{} is neither an attribute nor an item".format(item))


class GraphDict(DotDict):
    """A parent class for Vertices and Edges"""

    def _only_allow_vertex(self, value):
        if not isinstance(value, Vertex):
            raise TypeError("{} expected a Vertex object but got {}".format(
                self.__class__.__name__, type(value)))


class Vertices(GraphDict):
    """
    Stores vertices and synchronizes their `vertex_name` attribute with the key to keep graph, vertices, and edges all
    synchonized.
    """

    def __init__(self, owner):
        """
        args:
            owner (Graph): The graph to which these vertices belong.
        """
        super(Vertices, self).__init__()
        self._owner = owner

    def __setattr__(self, key, value):
        if key == '_owner' and isinstance(value, Graph):
            super(DotDict, self).__setattr__(key, value)
        else:
            super(Vertices, self).__setattr__(key, value)

    def __setitem__(self, key, value):
        self._only_allow_vertex(value)
        value.vertex_name = key
        value.parent_graph = self._owner
        super(Vertices, self).__setitem__(key, value)

    def to_hdf(self, hdf, group_name=None):
        """
        Save each vertex to HDF.

        Args:
            hdf (ProjectHDFio): HDF5 group object.
            group_name (str): HDF5 subgroup name. (Default is None.)
        """
        hdf5_server = open_if_group(hdf, group_name)
        hdf5_server["TYPE"] = str(type(self))

        for k, v in self.items():
            v.to_hdf(hdf5_server, k)

    def from_hdf(self, hdf, group_name=None):
        """
        Load each vertex from HDF.

        The base classes should all have been added by the graph, so we can iterate over self.

        Args:
            hdf (ProjectHDFio): HDF5 group object.
            group_name (str): HDF5 subgroup name. (Default is None.)
        """
        hdf5_server = open_if_group(hdf, group_name)
        for k, v in self.items():
            v.from_hdf(hdf5_server, k)
            self[k] = v


class Edges(GraphDict):
    """
    A nested dictionary of names specifying vertices, their states, and which vertex to go to next when leaving with
    a given state.
    """

    def __init__(self):
        super(Edges, self).__init__()

    def __getattribute__(self, item):
        return super(Edges, self).__getattribute__(item)

    def __setitem__(self, key, value):
        """Set vertex as a dead end -- all states lead to `None`."""
        self._only_allow_vertex(value)
        if key != value.vertex_name:
            raise ValueError("Edge dictionaries must have the same name as the vertex they are for. Expected {}"
                             "but got {}".format(value.vertex_name, key))
        super(Edges, self).__setitem__(key, DotDict({k: None for k in value.possible_vertex_states}))

    def initialize(self, vertex):
        self.__setitem__(vertex.vertex_name, vertex)

    def set_flow_chain(self, *args):
        """
        Create a chain of edges by specifying a series of vertices. If two consecutive vertices are provided, the edge
        runs along the first vertex's default state. Alternatively, the state to make the edge for can be explicitly
        specified by putting the appropriate string between two vertices in the arguments.

        Args:
            *args (Vertex/str): Vertex objects or a possible state of the previous argument.
        """

        for n, vertex in enumerate(args[:-1]):
            if not isinstance(vertex, Vertex):
                continue

            next_obj = args[n + 1]
            if isinstance(next_obj, str):
                state = next_obj
                next_vertex = args[n + 2]
            else:
                state = Vertex.DEFAULT_STATE
                next_vertex = args[n + 1]

            if not (isinstance(vertex, Vertex) and isinstance(next_vertex, Vertex)):
                raise TypeError("Edge flow must be between Vertex objects, but got {} and {}".format(
                    type(vertex), type(next_vertex)
                ))

            if state not in vertex.possible_vertex_states:
                raise KeyError("Got state {} which is not in possible states {} for vertex {}".format(
                    state, vertex.possible_vertex_states, vertex.vertex_name
                ))

            self[vertex.vertex_name][state] = next_vertex.vertex_name

    def to_hdf(self, hdf, group_name=None):
        """
        Save edges to HDF.

        Args:
            hdf (ProjectHDFio): HDF5 group object.
            group_name (str): HDF5 subgroup name. (Default is None.)
        """
        hdf5_server = open_if_group(hdf, group_name)
        hdf5_server["TYPE"] = str(type(self))
        for name, edge in self.items():
            generic_to_hdf(edge, hdf5_server, group_name=name)  # Edges are dictionaries, so use generic saver

    def from_hdf(self, hdf, group_name):
        """
        Edges should be created by the graph on instantiation, so we don't load anything. We only wrote them so they'd
        be readable in the HDF file without creating a python object.

        Args:
            hdf (ProjectHDFio): HDF5 group object.
            group_name (str): HDF5 subgroup name. (Default is None.)
        """
        pass


class WhitelistDict(DotDict):
    def __init__(self, value=None):
        if value is None:
            super(WhitelistDict, self).__init__()
        elif isinstance(value, (DotDict, dict)):
            for k, v in value.items():
                setattr(self, k, v)
        else:
            raise TypeError("WhitelistDict can only be instantiated using a dict or DotDict for initial values, but"
                            "got {}".format(type(value)))

    def __setattr__(self, key, value):
        if isinstance(value, (dict, DotDict)):
            value = WhitelistDict(value)
        self.__setitem__(key, value)


class Archive:
    """A convenience class for reading the HDF5 file of vertices."""

    ERROR_MESSAGE = "The graph {} cannot set values to its archive directly, the archive just reads the hdf file"

    def __init__(self, owner, whitelist, cache_length=10, clock=0):
        """

        Args:
            owner (Vertex): The vertex whose archived values we want to read.
        """
        if not isinstance(owner, Vertex):
            raise TypeError("An archive can only be the attribute of a Vertex, but got {} for owner type".format(
                type(owner)
            ))
        self._owner = owner
        self._input_cache = DotDict()
        self._output_cache = DotDict()
        self.whitelist = WhitelistDict(whitelist)
        if not hasattr(self.whitelist, 'input'):
            self.whitelist.input = {}
        if not hasattr(self.whitelist, 'output'):
            self.whitelist.output = {}
        self.cache_length = cache_length
        self.clock = clock

    def __getitem__(self, item):
        try:
            val = self._owner.hdf['archive/' + item]
        except KeyError:
            return None

        if isinstance(val, (ProjectHDFio, FileHDFio)):
            return val
        else:
            return None

    def __getattr__(self, item):
        return self.__getitem__(item)

    def __setitem__(self, key, value):
        raise RuntimeError(self.ERROR_MESSAGE.format(self._owner.get_graph_location()))

    def __setattr__(self, key, value):
        if key in ['_owner', 'cache_length', '_input_cache', '_output_cache', 'whitelist', 'clock']:
            super(Archive, self).__setattr__(key, value)
        else:
            raise RuntimeError(self.ERROR_MESSAGE.format(self._owner.get_graph_location()))

    def __str__(self):
        return str(self._owner['archive'])

    def update(self):
        clock = resolve_if_lazy(self.clock)
        print("UPDATE CLOCK", clock, type(clock))
        length = resolve_if_lazy(self.cache_length)

        for source, cache, whitelist, io_flag in zip(
                [self._owner.input, self._owner.output],
                [self._input_cache, self._output_cache],
                [self.whitelist.input, self.whitelist.output],
                ['input', 'output']
        ):

            for k, v in source.items():
                try:
                    period = resolve_if_lazy(whitelist[k])
                except KeyError:
                    continue

                if clock % period == 0:
                    val = v.resolve()
                    if io_flag == 'output':
                        val = val[-1]  # Only save the most recent output
                    try:
                        cache[k] += [val]
                        # TODO: Add comparer check between val and cache[k][-1]
                    except KeyError:
                        cache[k] = [val]

                if len(cache[k]) == length:
                    self._dump_cache(clock, io_flag, k, cache[k])

    def _update_cache(self):
        pass

    def _dump_cache(self, clock, io_flag, cache_name, cache):
        with self._owner.hdf.open('archive/{}/{}'.format(io_flag, cache_name)) as hdf_server:
            try:
                history = generic_from_hdf(hdf_server['data'], cache_name)
                history += cache
                generic_to_hdf(history, hdf_server['data'], cache_name)
                # TODO: Is there a more efficient way to append complex values to hdf?
                hdf_server['clock'] += [clock]
                print("Archiving for {}.{}.{}".format(self._owner.vertex_name, io_flag, cache_name))
            except ValueError:
                print(clock, type(clock))
                hdf_server['clock'] = [clock]
                with hdf_server.open('data') as data_server:
                    generic_to_hdf(cache, data_server, cache_name)
                print("Archive started for {}.{}.{}".format(self._owner.vertex_name, io_flag, cache_name))
        cache = []

    def finish(self):
        clock = resolve_if_lazy(self.clock)
        print("FINISH CLOCK", clock, type(clock))

        for source, cache, whitelist, io_flag in zip(
                [self._owner.input, self._owner.output],
                [self._input_cache, self._output_cache],
                [self.whitelist.input, self.whitelist.output],
                ['input', 'output']
        ):
            for k, v in source.items():
                try:
                    if len(cache[k]) > 0:
                        self._dump_cache(clock, io_flag, k, cache[k])
                        print("Final dump for {}.{}.{}".format(self._owner.vertex_name, io_flag, k))
                except KeyError:
                    pass