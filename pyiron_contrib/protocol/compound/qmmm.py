# coding: utf-8
# Copyright (c) Max-Planck-Institut für Eisenforschung GmbH - Computational Materials Design (CM) Department
# Distributed under the terms of "New BSD License", see the LICENSE file.

from __future__ import print_function

from pyiron_contrib.protocol.generic import PrimitiveVertex, Protocol
from pyiron_contrib.protocol.utils import ensure_iterable
from pyiron_contrib.protocol.primitive.one_state import ExternalHamiltonian, Counter, Norm, Max, GradientDescent
from pyiron_contrib.protocol.primitive.two_state import IsGEq, IsLEq
from pyiron_contrib.protocol.utils import Pointer, IODictionary
import numpy as np

"""
Protocol for running the force-based quantum mechanics/molecular mechanics concurrent coupling scheme described in 
Huber et al., Comp. Mat. Sci. 118 (2016) 259-268
"""

__author__ = "Dominik Gehringer, Liam Huber"
__copyright__ = "Copyright 2019, Max-Planck-Institut für Eisenforschung GmbH " \
                "- Computational Materials Design (CM) Department"
__version__ = "0.0"
__maintainer__ = "Liam Huber"
__email__ = "huber@mpie.de"
__status__ = "development"
__date__ = "June 6, 2019"


class QMMM(Protocol):
    """
    Relax a QM/MM coupled system.

    Needs an MM job (incl. superstructure), a QM job reference, QM target site ids, optional target site chemistry,
    optional domain ids, optional QM cell instructions?

    During setup, the domain indices are checked -- if None, build them from scratch using shells and the cell...stuff?

    Attributes:
        structure (pyiron.atomistics.structure.atoms.Atoms): The full region I+II superstructure.
        mm_ref_job_full_path (str): Path to the pyiron job to use for evaluating forces and energies of the MM domains.
        qm_ref_job_full_path (str): Path to the pyiron job to use for evaluating forces and energies of the QM domain.
        domain_ids (dict): A dictionary of ids, `seed`, `core`, `buffer`, and `filler`, for mapping atoms of the MM
            I+II superstructure to the various domains of the QM/MM coupling scheme. (Default is None, gets constructed
            using consecutive shells).
        seed_ids (int/list): The integer id (or ids) of atom(s) in the MM I+II superstructure to base the QM region on.
            These are the only atoms whose species can be changed. (Default is None, which raises an error unless
            `domain_ids` was explicitly provided -- else this parameter must be provided.)
        shell_cutoff (float): Maximum distance for two atoms to be considered neighbours in the construction of shells
            for automatic system partitioning. (Default is None, which will raise an error -- this parameter must be
            provided if domain partitioning is done using `seed_ids` and shells. When `domain_ids` are explicitly
            provided, `shell_cutoff` is not needed.)
        n_core_shells (int): How many neighbour shells around the seed(s) to relax using QM forces. (Default is 2.)
        n_buffer_shells (int): How many neighbour shells around the region I core to relax using MM forces. (Default is
            2.)
        seed_species (str/list): The species for each 'seed' atom in the QM domain. Value(s) should be a atomic symbol,
            e.g. 'Mg'. (Default is None, which leaves all seeds the same as they occur in the input structure.)
        vacuum_width (float): Minimum vacuum distance between atoms in two periodic images of the QM domain. Influences
            the final simulation cell for the QM calculation. (Default is 2.)
        filler_width (float): Length added to the bounding box of the region I atoms to create a new box from which to
            draw filler atoms using the initial MM I+II superstructure. Influences the final simulation cell for the QM
            calculation.If the value is not positive, no filler atoms are used. The second, larger box uses the same
            center as the bounding box of the QM region I, so this length is split equally to the positive and negative
            sides for each cartesian direction. (Default is 6.)
        n_steps (int): The maximum number of minimization steps to make. (Default is 100.)
        f_tol (float): The maximum force on any atom below which the calculation terminates. Only atoms which could be
            relaxed are considered, i.e. QM forces for region I core, and MM forces for region II and I buffer. (Filler
            atoms are not real, so we never care about their forces.) (Default is 1e-4 eV/angstrom.)
    """

    def __init__(self, project=None, name=None, job_name=None):
        self.setup = IODictionary()
        super(QMMM, self).__init__(project=project, name=name, job_name=job_name)

        self.protocol_finished += self._compute_qmmm_energy

        id_ = self.input.default
        id_.domain_ids = None
        id_.seed_ids = None
        id_.shell_cutoff = None
        id_.n_core_shells = 2
        id_.n_buffer_shells = 2
        id_.vacuum_width = 2.
        id_.filler_width = 6.

        id_.n_steps = 100
        id_.f_tol = 1e-4

        id_.gamma0 = 0.1
        id_.fix_com = True
        id_.use_adagrad = True

    def define_vertices(self):
        # Components
        g = self.graph
        g.partion = PartitionStructure()
        g.calc_static_mm = ExternalHamiltonian()
        g.calc_static_qm = ExternalHamiltonian()
        g.clock = Counter()
        g.force_norm_mm = Norm()
        g.force_norm_qm = Norm()
        g.max_force_mm = Max()
        g.max_force_qm = Max()
        g.check_force_mm = IsLEq()
        g.check_force_qm = IsLEq()
        g.check_steps = IsGEq()
        g.update_buffer_qm = AddDisplacements()
        g.update_core_mm = AddDisplacements()
        g.gradient_descent_mm = GradientDescent()
        g.gradient_descent_qm = GradientDescent()
        g.calc_static_small = ExternalHamiltonian()

    def define_execution_flow(self):
        g = self.graph
        g.make_pipeline(
            g.partion,
            g.check_steps, 'false',
            g.calc_static_mm,
            g.calc_static_qm,
            g.force_norm_mm,
            g.max_force_mm,
            g.check_force_mm, 'true',
            g.force_norm_qm,
            g.max_force_qm,
            g.check_force_qm, 'false',
            g.gradient_descent_mm,
            g.gradient_descent_qm,
            g.update_buffer_qm,
            g.update_core_mm,
            g.clock,
            g.check_steps
        )
        g.make_edge(g.check_force_mm, g.gradient_descent_mm, 'false')
        g.make_edge(g.check_force_qm, g.calc_static_small, 'true')
        g.make_edge(g.check_steps, g.calc_static_small, 'true')
        g.starting_vertex = g.partion
        g.restarting_vertex = g.clock

    def define_information_flow(self):
        gp = Pointer(self.graph)
        ip = Pointer(self.input)
        g = self.graph

        g.partition.input.structure = ip.structure
        g.partition.input.domain_ids = ip.domain_ids
        g.partition.input.seed_ids = ip.seed_ids
        g.partition.input.shell_cutoff = ip.shell_cutoff
        g.partition.input.n_core_shells = ip.n_core_shells
        g.partition.input.n_buffer_shells = ip.n_buffer_shells
        g.partition.input.vacuum_width = ip.vacuum_width
        g.partition.input.filler_width = ip.filler_width
        g.partition.input.seed_species = ip.seed_species

        g.calc_static_mm.input.ref_job_full_path = ip.mm_ref_job_full_path
        g.calc_static_mm.input.structure = gp.partition.output.mm_full_structure
        g.calc_static_mm.input.default.positions = gp.output.mm_full_structure.positions
        g.calc_static_mm.input.positions = gp.update_core_mm.output.positions[-1]

        g.calc_static_small.input.ref_job_full_path = ip.mm_ref_job_full_path
        g.calc_static_small.input.structure = gp.partition.output.mm_small_structure
        g.calc_static_small.input.default.positions = gp.partition.output.mm_small_structure.positions
        g.calc_static_small.input.positions = gp.update_core_mm.output.positions[-1]

        g.calc_static_qm.input.ref_job_full_path = ip.qm_ref_job_full_path
        g.calc_static_qm.input.structure = gp.partition.output.qm_structure
        g.calc_static_qm.input.default.positions = gp.partition.output.qm_structure.positions
        g.calc_static_qm.input.positions = gp.update_buffer_qm.output.positions[-1]

        g.check_steps.input.target = gp.clock.output.n_counts[-1]
        g.check_steps.input.threshold = ip.n_steps

        g.force_norm_mm.input.x = gp.calc_static_mm.output.forces[-1]
        g.force_norm_mm.input.ord = 2
        g.force_norm_mm.input.axis = -1

        g.max_force_mm.input.a = gp.force_norm_mm.output.n[-1]
        g.check_force_mm.input.target = gp.max_force_mm.output.amax[-1]
        g.check_force_mm.input.threshold = ip.f_tol

        g.force_norm_qm.input.x = gp.calc_static_qm.output.forces[-1][gp.partition.output.domain_ids_qm['only_core']]
        g.force_norm_qm.input.ord = 2
        g.force_norm_qm.input.axis = -1

        g.max_force_qm.input.a = gp.force_norm_qm.output.n[-1]
        g.check_force_qm.input.target = gp.max_force_qm.output.amax[-1]
        g.check_force_qm.input.threshold = ip.f_tol

        g.gradient_descent_mm.input.forces = gp.calc_static_mm.output.forces[-1]
        g.gradient_descent_mm.input.default.positions = gp.output.mm_full_structure.positions
        g.gradient_descent_mm.input.positions = gp.update_core_mm.output.positions[-1]
        g.gradient_descent_mm.input.masses = gp.output.mm_full_structure.get_masses
        g.gradient_descent_mm.input.mask = gp.partition.output.domain_ids['except_core']
        g.gradient_descent_mm.input.gamma0 = ip.gamma0
        g.gradient_descent_mm.input.fix_com = ip.fix_com
        g.gradient_descent_mm.input.use_adagrad = ip.use_adagrad

        g.gradient_descent_qm.input.forces = gp.calc_static_qm.output.forces[-1]
        g.gradient_descent_qm.input.default.positions = gp.partition.output.qm_structure.positions
        g.gradient_descent_qm.input.positions = gp.update_buffer_qm.output.positions[-1]
        g.gradient_descent_qm.input.masses = gp.partition.output.qm_structure.get_masses
        g.gradient_descent_qm.input.mask = gp.partition.output.domain_ids_qm['only_core']
        g.gradient_descent_qm.input.gamma0 = ip.gamma0
        g.gradient_descent_qm.input.fix_com = ip.fix_com
        g.gradient_descent_qm.input.use_adagrad = ip.use_adagrad

        g.update_core_mm.input.default.target = gp.partition.output.mm_full_structure.positions
        g.update_core_mm.input.target = gp.gradient_descent_mm.output.positions[-1]
        g.update_core_mm.input.target_mask = [
            gp.partition.output.domain_ids['seed'],
            gp.partition.output.domain_ids['core']
        ]
        g.update_core_mm.input.displacement = gp.gradient_descent_qm.output.displacements[-1]
        g.update_core_mm.input.displacement_mask = [
            gp.partition.output.domain_ids_qm['seed'],
            gp.partition.output.domain_ids_qm['core']
        ]

        g.update_buffer_qm.input.default.target = gp.partition.output.qm_structure.positions
        g.update_buffer_qm.input.target = gp.gradient_descent_qm.output.positions[-1]
        g.update_buffer_qm.input.target_mask = gp.partition.output.domain_ids_qm['buffer']
        g.update_buffer_qm.input.displacement = gp.gradient_descent_mm.output.displacements[-1]
        g.update_buffer_qm.input.displacement_mask = gp.partition.output.domain_ids['buffer']

    def _compute_qmmm_energy(self):
        gp = Pointer(self.graph)
        o = self.output
        o.energy_mm = gp.calc_static_mm.output.energy_pot[-1]
        o.energy_qm = gp.calc_static_qm.output.energy_pot[-1]
        o.energy_mm_one = gp.calc_static_small.output.energy_pot[-1]
        o.energy_qmmm = o.energy_mm + o.energy_qm - o.energy_mm_one

    def show_mm(self):
        try:
            mm_full_structure = self.graph.partition.output.mm_full_structure
            mm_full_structure.positions = self.graph.update_core.output.positions
            domain_ids = self.graph.partition.output.domain_ids
        except:
            raise  # Will figure out what to except later
            # partitioned = self.partition_input()
            # mm_full_structure = partitioned['mm_full_structure']
            # domain_ids = partitioned['domain_ids']

        color = 4 * np.ones(len(mm_full_structure))  # This 5th colour makes the balance of atoms
        for n, group in enumerate(['seed', 'core', 'buffer', 'filler']):
            color[domain_ids[group]] = n

        return mm_full_structure.plot3d(scalar_field=color)

    def show_qm(self):
        try:
            qm_structure = self.graph.partition.output.qm_structure
            qm_structure.positions = self.graph.update_buffer.output.positions
            domain_ids_qm = self.graph.partition.output.domain_ids_qm
        except:
            raise # Will figure out what to except later
            # partitioned = self.partition_input()
            # qm_structure = partitioned['qm_structure']
            # domain_ids_qm = partitioned['domain_ids_qm']

        color = 4 * np.ones(len(qm_structure))  # If you see this 5th colour, something is wrong
        for n, group in enumerate(['seed', 'core', 'buffer', 'filler']):
            color[domain_ids_qm[group]] = n

        return qm_structure.plot3d(scalar_field=color)

    def show_boxes(self):
        try:
            structure = self.graph.partition.output.structure
            qm_structure = self.graph.partition.output.qm_structure
        except:
            raise  # Will figure out what to except later
            # partitioned = self.partition_input()
            # structure = partitioned['structure']
            # qm_structure = partitioned['qm_structure']
        self._plot_boxes([structure.cell, qm_structure.cell],
                         colors=['r','b'],
                         titles=['MM Superstructure', 'QM Structure'])

    def partition_input(self):
        i = self.input
        return PartitionStructure.command(
            PartitionStructure, i.structure,
            i.domain_ids,
            i.seed_ids, i.shell_cutoff, i.n_core_shells, i.n_buffer_shells,
            i.vacuum_width, i.filler_width,
            i.seed_species
        )

    def _plot_boxes(self, cells, translate=None, colors=None, titles=None, default_color='b', size=(29, 21)):
        """
        Plots one or a list of cells in xy, yz and xt projection
        Args:
            cells (numpy.ndarray): The cells to plot. A list of (3,3) or a single (3,3) matrix
            translate (list): list of translations vectors for each cell list of (3,) numpy arrays.
            colors (list): list of colors for each cell in the list. e.g ['b', 'g', 'y'] or 'bgky'
            titles (list): list of names displayed for each cell. list of str
            default_color (str): if colors is not specified this color will be applied to all cells
            size (float,float): A tuple of two float specifiyng the size of the plot in centimeters

        Returns:
            matplotlib.figure: The figure which will be displayed
        """
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D

        # Make sure that looping over this properties works smoothly
        cells = ensure_iterable(cells)
        translate = translate or [np.zeros(3,)]*len(cells)
        colors = colors or [default_color]*len(cells)
        titles = ensure_iterable(titles)

        # Scaled coordinates for all edges of the cell
        edges = [
            [(0, 0, 0), (1, 0, 0)],
            [(0, 0, 0), (0, 1, 0)],
            [(0, 0, 0), (0, 0, 1)],
            [(1, 1, 0), (1, 0, 0)],
            [(1, 1, 0), (0, 1, 0)],
            [(1, 1, 0), (1, 1, 1)],
            [(1, 0, 1), (1, 0, 0)],
            [(1, 0, 1), (1, 1, 1)],
            [(1, 0, 1), (0, 0, 1)],
            [(0, 1, 1), (1, 1, 1)],
            [(0, 1, 1), (0, 0, 1)],
            [(0, 1, 1), (0, 1, 0)]
        ]

        # Map axis to indices
        axis_mapping = {a: i for a, i in zip('xyz', range(3))}
        # Planes to plot
        planes = ['xy', 'yz', 'xz']
        # Get subplots and set the size in inches
        fig, axes = plt.subplots(1, len(planes))
        fig.set_size_inches(*[s / 2.54 for s in size])

        for i, plane in enumerate(planes):
            axis = axes[i]
            axis.set_aspect('equal')
            axis.title.set_text('{} plane'.format(plane))
            indices = [axis_mapping[a] for a in plane]
            for j, (cell, color, translation) in enumerate(zip(cells, colors, translate)):
                calc = lambda mul_, cell_: sum([m_ * vec_ for m_, vec_ in zip(mul_, cell_)])
                coords = [(calc(start, cell) + translation, calc(end, cell) + translation) for start, end in edges]
                for start, end in coords:
                    sx, sy = start[indices]
                    ex, ey = end[indices]
                    axis.plot([sx, ex], [sy, ey], color=color)
        # Create dummy legend outside
        legend_lines = [Line2D([0], [0], color=col or default_color, lw=2) for col in colors]
        legend_titles = [title or 'Box {}'.format(i + 1) for i, title in enumerate(titles)]
        plt.figlegend(legend_lines, legend_titles, loc='lower center', fancybox=True, shadow=True)
        return plt


class AddDisplacements(PrimitiveVertex):

    def __init__(self, name=None):
        super(AddDisplacements, self).__init__(name=name)
        self.input.default.target_mask = None
        self.input.default.displacement_mask = None

    def command(self, target, displacement, target_mask, displacement_mask):
        result = target.copy()
        if target_mask is not None and isinstance(target_mask, list):
            target_mask = np.concatenate(target_mask)
        if displacement_mask is not None and isinstance(displacement_mask, list):
            displacement_mask = np.concatenate(displacement_mask)
        if target_mask is not None and displacement_mask is not None:
            result[target_mask] += displacement[displacement_mask]
        elif target_mask is not None and displacement_mask is None:
            result[target_mask] += displacement
        elif target_mask is None and displacement_mask is not None:
            result += displacement[displacement_mask]
        else:
            result += displacement
        return {
            'positions': result
        }


class PartitionStructure(PrimitiveVertex):
    """

    """
    def __init__(self, name=None):
        super(PartitionStructure, self).__init__(name=name)
        id_ = self.input.default
        id_.domain_ids = None
        id_.seed_ids = None
        id_.shell_cutoff = None
        id_.n_core_shells = None
        id_.n_buffer_shells = None
        id_.filler_width = None

    def command(
            self, structure,
            domain_ids,
            seed_ids, shell_cutoff, n_core_shells, n_buffer_shells,
            vacuum_width, filler_width,
            seed_species
    ):
        domain_ids, domain_ids_qm, mm_small_structure = self._set_qm_structure(
            structure,
            domain_ids,
            seed_ids, shell_cutoff, n_core_shells, n_buffer_shells,
            vacuum_width, filler_width
        )
        qm_structure = self._change_qm_species(mm_small_structure, domain_ids_qm, seed_species)
        domain_ids_qm['only_core'] = self._only_core(domain_ids_qm)
        domain_ids['except_core'] = self._except_core(structure, domain_ids)
        return {
            'mm_full_structure': structure,
            'mm_small_structure': mm_small_structure,
            'qm_structure': qm_structure,
            'domain_ids': domain_ids,
            'domain_ids_qm': domain_ids_qm
        }

    def _set_qm_structure(
            self,
            superstructure,
            domain_ids,
            seed_ids, shell_cutoff, n_core_shells, n_buffer_shells,
            vacuum_width, filler_width
    ):
        if domain_ids is not None and seed_ids is not None:
            raise ValueError('Only *one* of `seed_ids` and `domain_ids` may be provided.')
        elif domain_ids is not None:
            seed_ids = domain_ids['seed']
            core_ids = domain_ids['core']
            buffer_ids = domain_ids['buffer']
            filler_ids = domain_ids['filler']
            region_I_ids = np.concatenate((seed_ids, core_ids, buffer_ids))
            bb = self._get_bounding_box(superstructure[np.concatenate((region_I_ids, filler_ids))])
        elif seed_ids is not None:
            shells = self._build_shells(superstructure, n_core_shells + n_buffer_shells, shell_cutoff, seed_ids)
            core_ids = np.concatenate(shells[:n_core_shells])
            buffer_ids = np.concatenate(shells[n_core_shells:])
            region_I_ids = np.concatenate((seed_ids, core_ids, buffer_ids))
            bb = self._get_bounding_box(superstructure[region_I_ids])
            extra_box = 0.5 * np.array(filler_width)
            bb[:, 0] -= extra_box
            bb[:, 1] += extra_box

            # Store it because get bounding box return a tight box and is different
            filler_ids = self._get_ids_within_box(superstructure, bb)
            filler_ids = np.setdiff1d(filler_ids, region_I_ids)

            bb = self._get_bounding_box(superstructure[np.concatenate((region_I_ids, filler_ids))])

            domain_ids = {'seed': seed_ids, 'core': core_ids, 'buffer': buffer_ids, 'filler': filler_ids}
        else:
            raise ValueError('At least *one* of `seed_ids` and `domain_ids` must be provided.')


        # Build the domain ids in the qm structure
        qm_structure = None
        domain_ids_qm = {}
        offset = 0
        for key, ids in domain_ids.items():
            if qm_structure is None:
                qm_structure = superstructure[ids]
            else:
                qm_structure += superstructure[ids]
            id_length = len(ids)
            domain_ids_qm[key] = np.arange(id_length) + offset
            offset += id_length

        # And put everything in a box near (0,0,0)
        extra_vacuum = 0.5 * vacuum_width
        bb[:, 0] -= extra_vacuum
        bb[:, 1] += extra_vacuum

        # If the bounding box is larger than the MM superstructure
        bs = np.abs(bb[:, 1] - bb[:, 0])
        supercell_lengths = [np.linalg.norm(row) for row in superstructure.cell]
        shrinkage = np.array([(box_size - cell_size) / 2.0 if box_size > cell_size else 0.0
                              for box_size, cell_size in zip(bs, supercell_lengths)])
        if np.any(shrinkage > 0):
            self.logger.info('The QM box is larger than the MM Box therefore I\'ll shrink it')
            bb[:, 0] += shrinkage
            bb[:, 1] -= shrinkage
        elif any([0.9 < box_size / cell_size < 1.0 for box_size, cell_size in zip(bs, supercell_lengths)]):
            # Check if the box is just slightly smaller than the superstructure cell
            self.logger.warn(
                'Your cell is nearly as large as your supercell. Probably you want to expand it a little bit')


        qm_structure.cell = np.identity(3) * np.ptp(bb, axis=1)

        box_center = tuple(np.dot(np.linalg.inv(superstructure.cell), np.mean(bb, axis=1)))
        qm_structure.wrap(box_center)
        # Wrap it to the unit cell
        qm_structure.positions = np.dot(qm_structure.get_scaled_positions(), qm_structure.cell)

        return domain_ids, domain_ids_qm, qm_structure

    @staticmethod
    def _change_qm_species(qm_structure, domain_ids_qm, seed_species):
        # The seed sites are the first in the qm structure

        for index, species in zip(domain_ids_qm['seed'], seed_species):
            qm_structure[index] = species
        return qm_structure

    @staticmethod
    def _get_ids_within_box(structure, box):
        """
        Finds all the atoms in a structure who have a periodic image inside the bounding box.

        Args:
            structure (Atoms): The structure to search.
            box (np.ndarray): A 3x2 array of the x-min through z-max values for the bounding rectangular prism.

        Returns:
            np.ndarray: The integer ids of the atoms inside the box.
        """
        box_center = np.mean(box, axis=1)
        box_center_direct = np.dot(np.linalg.inv(structure.cell), box_center)
        # Wrap atoms so that they are the closest image to the box center
        wrapped_structure = structure.copy()
        wrapped_structure.wrap(tuple(box_center_direct))
        pos = wrapped_structure.positions
        # Keep only atoms inside the box limits
        masks = []
        for d in np.arange(len(box)):
            masks.append(pos[:, d] > box[d, 0])
            masks.append(pos[:, d] < box[d, 1])
        total_mask = np.prod(masks, axis=0).astype(bool)
        return np.arange(len(structure), dtype=int)[total_mask]

    @staticmethod
    def _build_shells(structure, n_shells, shell_cutoff, seed_ids):
        indices = [seed_ids]
        current_shell_ids = seed_ids
        for _ in range(n_shells):
            neighbors = structure.get_neighbors(id_list=current_shell_ids, cutoff=shell_cutoff)
            new_ids = np.unique(neighbors.indices)
            # Make it exclusive
            for shell_ids in indices:
                new_ids = np.setdiff1d(new_ids, shell_ids)
            indices.append(new_ids)
            current_shell_ids = new_ids
        # Pop seed ids
        indices.pop(0)
        return indices

    @staticmethod
    def _get_bounding_box(structure):
        """
        Finds the smallest rectangular prism which encloses all atoms in the structure after accounting for periodic
        boundary conditions.

        So what's the problem?

        |      ooooo  |, easy, wrap by CoM (centre of mass)
        |ooo        oo|, easy, wrap by CoM
        |o    ooo    o|, uh-oh! Need this function.


        Args:
            structure (Atoms): The structure to bound.

        Returns:
            numpy.ndarray: A 3x2 array of the x-min through z-max values for the bounding rectangular prism.
        """
        wrapped_structure = structure.copy()
        # Take the frist positions and wrap the atoms around there to determine the size of the bounding box
        wrap_center = tuple(np.dot(np.linalg.inv(structure.cell), structure.positions[0, :]))
        wrapped_structure.wrap(wrap_center)

        bounding_box = np.vstack([
            np.amin(wrapped_structure.positions, axis=0),
            np.amax(wrapped_structure.positions, axis=0)
        ]).T
        return bounding_box

    @staticmethod
    def _except_core(structure, domain_ids):
        seed_ids = np.array(domain_ids['seed'])
        core_ids = np.array(domain_ids['core'])
        return np.setdiff1d(np.arange(len(structure)), np.concatenate([seed_ids, core_ids]))

    @staticmethod
    def _only_core(domain_ids_qm):
        return np.concatenate([domain_ids_qm['seed'], domain_ids_qm['core']])