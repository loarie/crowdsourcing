"""
MIT License

Copyright (c) 2017 Grant Van Horn

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
# pylint: disable=line-too-long

from __future__ import absolute_import, division, print_function

from collections import Counter
import math
import random

import numpy as np

from scipy.linalg import block_diag
from scipy.sparse import block_diag as sparse_block_diag

from ...crowdsourcing import CrowdDataset, CrowdImage, CrowdWorker, CrowdLabel
from ...util.taxonomy import Taxonomy

class CrowdDatasetMulticlassSingleBinomial(CrowdDataset):
    """ A dataset for multiclass labeling across a taxonomy.
    """

    def __init__(self,

                 # Taxonomy of label nodes
                 taxonomy=None,

                 # Prior probability on the classes: p(y)
                 # An iterable, {class_key : probability of class}
                 class_probs=None, # This will be computed from the taxonomy.
                 # Global priors for the probability of a class occuring,
                 # used to estimate the class priors
                 class_probs_prior_beta=10,
                 # An iterable, {class_key : prior probability of class}
                 class_probs_prior=None, # This will be computed from the taxonomy.

                 # Global priors used to compute the pooled probabilities for a
                 # worker being correct
                 prob_correct_prior_beta=15,
                 prob_correct_prior=0.8,
                 prob_correct_beta=10,
                 prob_correct=0.8,

                 # Global priors used to compute worker trust
                 prob_trust_prior_beta=15,
                 prob_trust_prior=0.8,
                 prob_trust_beta=10,
                 prob_trust=0.8,

                 # TODO: refactor to `verification_task`
                 model_worker_trust=False,
                 # TODO: refactor to `dependent_verification`
                 recursive_trust=True,
                 **kwargs):

        super(CrowdDatasetMulticlassSingleBinomial, self).__init__(**kwargs)
        self._CrowdImageClass_ = CrowdImageMulticlassSingleBinomial
        self._CrowdWorkerClass_ = CrowdWorkerMulticlassSingleBinomial
        self._CrowdLabelClass_ = CrowdLabelMulticlassSingleBinomial

        #self.class_probs = class_probs
        self.class_probs_prior_beta = class_probs_prior_beta
        #self.class_probs_prior = class_probs_prior

        # These are the dataset wide priors that we use to estimate the per
        # worker skill parameters
        self.taxonomy = taxonomy
        self.prob_correct_prior_beta = prob_correct_prior_beta
        self.prob_correct_prior = prob_correct_prior
        self.prob_correct_beta = prob_correct_beta
        self.prob_correct = prob_correct

        # Worker trust probabilities
        self.prob_trust_prior_beta = prob_trust_prior_beta
        self.prob_trust_prior = prob_trust_prior
        self.prob_trust_beta = prob_trust_beta
        self.prob_trust = prob_trust

        self.model_worker_trust = model_worker_trust
        self.recursive_trust = recursive_trust

        # NOTE: not sure what to do here, one for each class?
        # How should it be plotted?
        self.skill_names = ['Prob Correct']
        if model_worker_trust:
            self.skill_names.append('Prob Trust')

        self.encode_exclude['taxonomy'] = True

    def copy_parameters_from(self, dataset, full=True):
        super(CrowdDatasetMulticlassSingleBinomial, self).copy_parameters_from(
            dataset,
            full=full
        )

        raise NotImplemented()

        self.class_probs = dataset.class_probs
        self.class_probs_prior_beta = dataset.class_probs_prior_beta
        self.class_probs_prior = dataset.class_probs_prior

        self.taxonomy = dataset.taxonomy.duplicate(duplicate_data=True)
        self.taxonomy.finalize()
        if hasattr(dataset.taxonomy, 'priors_initialized'):
            self.taxonomy.priors_initialized = \
                dataset.taxonomy.priors_initialized

        self.prob_correct_prior_beta = dataset.prob_correct_prior_beta
        self.prob_correct_prior = dataset.prob_correct_prior

        self.prob_correct_beta = dataset.prob_correct_beta

        self.prob_trust_prior_beta = dataset.prob_trust_prior_beta
        self.prob_trust_prior = dataset.prob_trust_prior
        self.prob_trust_beta = dataset.prob_trust_beta
        self.prob_trust = dataset.prob_trust

        self.model_worker_trust = dataset.model_worker_trust
        self.recursive_trust = dataset.recursive_trust

        self.estimate_priors_automatically = \
            dataset.estimate_priors_automatically

    def initialize_default_priors(self):
        """ Convenience function for initializing all the priors to default
        values.
        """
        # All nodes will be initialized to have `prob_correct_prior`
        for node in self.taxonomy.breadth_first_traversal():
            node.data['prob'] = 0.
            if not node.is_leaf:
                node.data['prob_correct_prior'] = self.prob_correct_prior
                node.data['prob_correct'] = self.prob_correct_prior

        # Initialize the node probabilities (of occuring)
        for leaf_node in self.taxonomy.leaf_nodes():
            prob_y = self.class_probs[leaf_node.key]
            leaf_node.data['prob'] = prob_y
            # Update the node distributions
            for ancestor in leaf_node.ancestors:
                ancestor.data['prob'] += prob_y

        self.taxonomy.priors_initialized = True

    def initialize_data_structures(self):
        """ Build data structures to speed up processing.
        """

        assert self.taxonomy.finalized, "The taxonomy must be finalized."
        assert self.taxonomy.priors_initialized, "The taxonomy priors must be initialized."

        # for node in self.taxonomy.breadth_first_traversal():
        #     print(node.key)
        #     print("\t" + ", ".join(node.children.keys()))
        #     print

        # We are going to remap the taxonomy to a flat vector
        orig_node_key_to_integer_id = {}
        integer_id_to_orig_node_key = {}
        # We want the inner nodes to be [0, num inner nodes) integer ids
        # This is because its the inner nodes that contain the skill values
        for i, node in enumerate(self.taxonomy.inner_nodes()):
            orig_node_key_to_integer_id[node.key] = i
            integer_id_to_orig_node_key[i] = node.key
        num_inner_nodes = i + 1
        id_offset = i + 1

        # Add the leaf nodes
        leaf_node_key_set = set()
        leaf_integer_ids = []
        for i, node in enumerate(self.taxonomy.leaf_nodes()):
            leaf_node_key_set.add(node.key)
            integer_id = i + id_offset
            orig_node_key_to_integer_id[node.key] = integer_id
            integer_id_to_orig_node_key[integer_id] = node.key
            leaf_integer_ids.append(integer_id)
        self.orig_node_key_to_integer_id = orig_node_key_to_integer_id
        self.integer_id_to_orig_node_key = integer_id_to_orig_node_key
        self.leaf_integer_ids = np.array(leaf_integer_ids, dtype=np.intp)
        self.leaf_node_key_set = leaf_node_key_set
        self.leaf_node_keys = list(leaf_node_key_set)

        # kis = orig_node_key_to_integer_id.items()
        # kis.sort(key=lambda x: x[1])
        # for k, i in kis:
        #     print("%s : %d" % (k, i))

        # Now its important that the remaining data structures respect the ordering
        # that we just created. Namely, breadth first traversal might not give the
        # correct ordering anymore, so we want to just use the integer ids.
        num_nodes = len(self.taxonomy.nodes)

        # Create a node prior vector
        node_priors = np.zeros(num_nodes, dtype=np.float32)
        for integer_id in xrange(num_nodes):
            k = integer_id_to_orig_node_key[integer_id]
            node = self.taxonomy.nodes[k]
            node_priors[integer_id] = node.data['prob']
        self.node_priors = node_priors

        # For each parent node this stores the parent index and children indices
        # First value is the parent index, remaining values are the sibling indices. This is used to construct
        # the M matrix for the workers
        parent_and_siblings_indices = []
        for integer_id in xrange(num_inner_nodes):
            k = integer_id_to_orig_node_key[integer_id]
            parent_node = self.taxonomy.nodes[k]
            sibling_integer_ids = [orig_node_key_to_integer_id[k] for k in parent_node.children]
            #sibling_integer_ids.sort()
            ps = [integer_id] + sibling_integer_ids
            parent_and_siblings_indices.append(ps)
        self.parent_and_siblings_indices = parent_and_siblings_indices



        # For each child node this store the parent index
        # This is used to construct the N matrix for workers.
        parent_indices = []
        for integer_id in xrange(num_nodes):
            k = integer_id_to_orig_node_key[integer_id]
            node = self.taxonomy.nodes[k]
            if not node.is_root:
                parent_integer_id = orig_node_key_to_integer_id[node.parent.key]
                parent_indices.append(parent_integer_id)
        self.parent_indices = parent_indices

        num_classes = self.taxonomy.num_leaf_nodes
        max_path_length = self.taxonomy.max_depth + 1

        # Path from the root node to each possible classification node
        # Pad with negative 1 so that each path length is the same.
        class_node_index_list_padded_neg_1 = np.ones([num_classes, max_path_length], np.intp) * -1

        # Path from the root node to each possible annotation node
        # We'll pad with -2 in this case to prevent matching the -1 padding from `class_node_index_list_padded_neg_1`
        anno_node_index_list_padded_neg_2 = np.ones([num_nodes, max_path_length], dtype=np.intp) * -2

        class_index = 0
        root_to_node_path_list = {}
        for integer_id in xrange(num_nodes):
            k = integer_id_to_orig_node_key[integer_id]
            node = self.taxonomy.nodes[k]

            # Path from the parent node to root
            orig_path = [ancestor.key for ancestor in node.ancestors]
            # Path from the root to the parent node
            orig_path.reverse()
            # Path from the root to the node
            orig_path += [node.key]
            # Get the integer ids for this path
            integer_id_path = [orig_node_key_to_integer_id[k] for k in orig_path]
            # Sanity checking the values. All inner nodes should have an index less than the total number of inner nodes.
            if len(integer_id_path[:-1]):
                assert max(integer_id_path[:-1]) < self.taxonomy.num_inner_nodes

            anno_node_index_list_padded_neg_2[integer_id, :len(integer_id_path)] = integer_id_path
            root_to_node_path_list[integer_id] = integer_id_path

            # Store the class labels in their own matrix as well
            if node.is_leaf:
                class_node_index_list_padded_neg_1[class_index, :len(integer_id_path)] = integer_id_path
                class_index += 1
        self.class_node_index_list_padded_neg_1 = class_node_index_list_padded_neg_1
        self.anno_node_index_list_padded_neg_2 = anno_node_index_list_padded_neg_2
        self.root_to_node_path_list = root_to_node_path_list

        # These are precomputed index lists that will allow us to copy over values from M and N into a
        # annotation probability tensor
        annotation_probs_insert_from_M = [[], [], []]
        annotation_probs_M_indices = [[], []]

        annotation_probs_insert_from_N = [[], [], []]
        annotation_probs_N_indices = [[]]

        # Build up the integer array indices to fill in annotation_probs
        # For each class label y
        for y in xrange(num_classes):
            # Get the node index path from the root node to y
            path_to_y = class_node_index_list_padded_neg_1[y]

            # Consider each possible annotation z
            for z in xrange(1, num_nodes): # We can ignore annotations at the root.
                # Get the node index path from the root node to z
                path_to_z = anno_node_index_list_padded_neg_2[z]
                z_depth = np.sum(path_to_z >= 0)
                z_level = z_depth - 1

                # For each node on the path from root->y and root->z,
                # select from either M or N depending on if the nodes match
                for i, (y_node_index, z_node_index) in enumerate(zip(path_to_y[:z_level], path_to_z[:z_level])):

                    # The nodes match
                    if y_node_index == z_node_index:
                        # Index into M using the children indices
                        y_node_child_index = path_to_y[i+1]
                        z_node_child_index = path_to_z[i+1]

                        assert y_node_child_index > 0
                        assert z_node_child_index > 0

                        # Add the indices of where we will access M to get the probability value
                        annotation_probs_M_indices[0].append(y_node_child_index)
                        annotation_probs_M_indices[1].append(z_node_child_index)

                        # Add the indices of where we will store the value of M into the annotation_probs matrix
                        annotation_probs_insert_from_M[0].append(y)
                        annotation_probs_insert_from_M[1].append(z)
                        annotation_probs_insert_from_M[2].append(i)

                    # The nodes do not match and the path has not ended
                    elif z_node_index >= 0:

                        # index into N using the child of z
                        z_node_child_index = path_to_z[i+1]

                        # If we haven't reached level(z), and the parent is not negative,
                        # then the child should not be negative either.
                        assert z_node_child_index >= 0

                        # Add the indices of where we will access N to get the probability value
                        annotation_probs_N_indices[0].append(z_node_child_index)

                        # Add the indices of where we will store the value of N into the annotation_probs matrix
                        annotation_probs_insert_from_N[0].append(y)
                        annotation_probs_insert_from_N[1].append(z)
                        annotation_probs_insert_from_N[2].append(i)

        self.annotation_probs_insert_from_M = np.array(annotation_probs_insert_from_M, dtype=np.intp)
        self.annotation_probs_M_indices = np.array(annotation_probs_M_indices, dtype=np.intp)
        self.annotation_probs_insert_from_N = np.array(annotation_probs_insert_from_N, dtype=np.intp)
        self.annotation_probs_N_indices = np.array(annotation_probs_N_indices, dtype=np.intp)


        # Construct index arrays that will be used to create the M matrix
        skill_vector_correct_read_indices = []
        M_correct_rows = []
        M_correct_cols = []
        skill_vector_incorrect_read_indices = []
        skill_vector_node_priors_read_indices = []
        M_incorrect_rows = []
        M_incorrect_cols = []
        for node_indices in parent_and_siblings_indices:
            parent_index = node_indices[0]
            sibling_indices = node_indices[1:]
            num_siblings = len(sibling_indices)

            # Copy the parent node skill to the children (at the diagonal)
            skill_vector_correct_read_indices += [parent_index] * num_siblings
            M_correct_rows += sibling_indices
            M_correct_cols += sibling_indices

            for r in sibling_indices:
                for c in sibling_indices:
                    if r == c:
                        continue

                    # M[r, c] is the probability worker says c when the ground truth is r
                    skill_vector_incorrect_read_indices.append(parent_index)
                    skill_vector_node_priors_read_indices.append(c)
                    M_incorrect_rows.append(r)
                    M_incorrect_cols.append(c)

        self.skill_vector_correct_read_indices = np.array(skill_vector_correct_read_indices, np.intp)
        self.M_correct_rows = np.array(M_correct_rows, np.intp)
        self.M_correct_cols = np.array(M_correct_cols, np.intp)
        self.skill_vector_incorrect_read_indices = np.array(skill_vector_incorrect_read_indices, np.intp)
        self.skill_vector_node_priors_read_indices = np.array(skill_vector_node_priors_read_indices, np.intp)
        self.M_incorrect_rows = np.array(M_incorrect_rows, np.intp)
        self.M_incorrect_cols = np.array(M_incorrect_cols, np.intp)

        # Construct a vector holding the default skill priors for a worker
        self.default_skill_vector = np.ones(self.taxonomy.num_inner_nodes, dtype=np.float32) * self.prob_correct
        self.pooled_prob_correct_vector = np.ones(self.taxonomy.num_inner_nodes, dtype=np.float32) * self.prob_correct_prior

        self.encode_exclude['orig_node_key_to_integer_id'] = True
        self.encode_exclude['integer_id_to_orig_node_key'] = True
        self.encode_exclude['leaf_integer_ids'] = True
        self.encode_exclude['leaf_node_key_set'] = True
        self.encode_exclude['leaf_node_keys'] = True
        self.encode_exclude['node_priors'] = True
        self.encode_exclude['parent_and_siblings_indices'] = True
        self.encode_exclude['parent_indices'] = True
        self.encode_exclude['class_node_index_list_padded_neg_1'] = True
        self.encode_exclude['anno_node_index_list_padded_neg_2'] = True
        self.encode_exclude['annotation_probs_insert_from_M'] = True
        self.encode_exclude['annotation_probs_M_indices'] = True
        self.encode_exclude['annotation_probs_insert_from_N'] = True
        self.encode_exclude['annotation_probs_N_indices'] = True

        self.encode_exclude['skill_vector_correct_read_indices'] = True
        self.encode_exclude['M_correct_rows'] = True
        self.encode_exclude['M_correct_cols'] = True
        self.encode_exclude['skill_vector_incorrect_read_indices'] = True
        self.encode_exclude['skill_vector_node_priors_read_indices'] = True
        self.encode_exclude['M_incorrect_rows'] = True
        self.encode_exclude['M_incorrect_cols'] = True

        self.encode_exclude['default_skill_vector'] = True
        self.encode_exclude['pooled_prob_correct_vector'] = True

    def estimate_priors(self, gt_dataset=None):
        """Estimate the dataset-wide parameters.
        For the full dataset (given a gt_dataset) we want to estimate the class
        priors.
        """

        assert False, "Not implemented"

        # Initialize the `prob_correct_prior` for each node to
        # `self.prob_correct_prior`
        if not self.taxonomy.priors_initialized:
            print("INITIALIZING all node priors to defaults")
            self.initialize_default_priors()
            self.taxonomy.priors_initialized = True

        # Pooled counts
        for node in self.taxonomy.breadth_first_traversal():
            if not node.is_leaf:
                # [num, denom] => [# correct, # total]
                node.data['prob_correct_counts'] = [0, 0]
                node.data['prob'] = 0

        # Counts for the classes
        class_dist = {node.key: 0. for node in self.taxonomy.leaf_nodes()}

        # Go through each image and add to the counts
        for i in self.images:

            # Does this image have a computer vision annotation?
            has_cv = 0
            if self.cv_worker and self.cv_worker.id in self.images[i].z:
                has_cv = 1

            # Skip this image if it doesn't have at least human annotations.
            if len(self.images[i].z) - has_cv <= 1:
                continue

            # If we have access to a ground truth dataset, then use the label
            # from there.
            if gt_dataset is not None:
                y = gt_dataset.images[i].y.label
            # Otherwise, grab the current prediction for the image
            else:
                y = self.images[i].y.label

            # Update the class distributions
            class_dist[y] += 1.

            y_node = self.taxonomy.nodes[y]
            y_level = y_node.level

            # Go through each worker and add their annotation to the respective
            # counts.
            for w in self.images[i].z:
                # Skip the computer vision annotations
                if not self.images[i].z[w].is_computer_vision():

                    # Worker annotation
                    z = self.images[i].z[w].label
                    z_node = self.taxonomy.nodes[z]
                    z_level = z_node.level

                    # Update the counts for each layer of the taxonomy.
                    for l in xrange(0, y_level):

                        # Get the ancestor at level `l` and the child at `l+1`
                        # for the image label
                        y_l_node = self.taxonomy.node_at_level_from_node(l, y_node)
                        y_l_child_node = self.taxonomy.node_at_level_from_node(l + 1, y_node)

                        # Update the denominator for prob_correct
                        y_l_node.data['prob_correct_counts'][1] += 1.

                        if l < z_level:

                            # Get the child at `l+1` for the worker's prediction
                            z_l_child_node = self.taxonomy.node_at_level_from_node(l + 1, z_node)

                            # Are the children nodes the same? If so then the worker
                            # was correct and we update the parent node
                            if z_l_child_node == y_l_child_node:
                                # Update the numerator for prob_correct
                                y_l_node.data['prob_correct_counts'][0] += 1.


        # compute the pooled probability of being correct priors
        for node in self.taxonomy.breadth_first_traversal():
            if not node.is_leaf:

                # Probability of predicting the children of a node correctly
                prob_correct_prior = node.data['prob_correct_prior']
                prob_correct_num = self.prob_correct_prior_beta * prob_correct_prior + node.data['prob_correct_counts'][0]
                prob_correct_denom = self.prob_correct_prior_beta + node.data['prob_correct_counts'][1]
                prob_correct_denom = np.clip(prob_correct_denom, a_min=0.00000001, a_max=None)
                node.data['prob_correct'] = np.clip(prob_correct_num / prob_correct_denom, a_min=0.00000001, a_max=0.99999)

        # Class probabilities (leaf node probabilities)
        num_images = float(np.sum(class_dist.values()))
        for y, count in class_dist.iteritems():
            num = self.class_probs_prior[y] * self.class_probs_prior_beta + count
            denom = self.class_probs_prior_beta + num_images
            self.class_probs[y] = np.clip(num / denom, a_min=0.00000001, a_max=0.999999)

        # Node probabilities:
        for leaf_node in self.taxonomy.leaf_nodes():
            prob_y = self.class_probs[leaf_node.key]
            leaf_node.data['prob'] = prob_y
            # Update the node distributions
            for ancestor in leaf_node.ancestors:
                ancestor.data['prob'] += prob_y

        # Probability of a worker trusting previous annotations
        # (with a Beta prior)
        if self.model_worker_trust:
            prob_trust_num = self.prob_trust_prior_beta * self.prob_trust_prior
            prob_trust_denom = self.prob_trust_prior_beta

            for worker_id, worker in self.workers.iteritems():
                for image in worker.images.itervalues():

                    if self.recursive_trust:
                        # Only dependent on the imediately previous value
                        worker_t = image.z.keys().index(worker_id)
                        if worker_t > 0:
                            worker_label = image.z[worker_id].label
                            prev_anno = image.z.values()[worker_t - 1]

                            prob_trust_denom += 1.
                            if worker_label == prev_anno.label:
                                prob_trust_num += 1.
                    else:
                        # Assume all of the previous labels are treated
                        # independently
                        worker_label = image.z[worker_id].label
                        for prev_worker_id, prev_anno in image.z.iteritems():
                            if prev_worker_id == worker_id:
                                break
                            if not prev_anno.is_computer_vision() or self.naive_computer_vision:
                                prob_trust_denom += 1.
                                if worker_label == prev_anno.label:
                                    prob_trust_num += 1.

            self.prob_trust = np.clip(prob_trust_num / float(prob_trust_denom), 0.00000001, 0.9999)

    def initialize_parameters(self, avoid_if_finished=False):
        """Pass on the dataset-wide worker skill priors to the workers.
        """

        for worker in self.workers.itervalues():
            if avoid_if_finished and worker.finished:
                #if worker.M is None:
                #    worker.build_M_and_N() # make sure the worker has their M and N matrices
                continue

            if self.model_worker_trust:
                worker.prob_trust = self.prob_trust

            worker.skill_vector = np.copy(self.default_skill_vector)
            #worker.build_M_and_N()

    def parse(self, data):
        super(CrowdDatasetMulticlassSingleBinomial, self).parse(data)
        if 'taxonomy_data' in data:
            self.taxonomy = Taxonomy()
            self.taxonomy.load(data['taxonomy_data'])
            self.taxonomy.finalize()

    def encode(self):
        data = super(CrowdDatasetMulticlassSingleBinomial, self).encode()
        if self.taxonomy is not None:
            data['taxonomy_data'] = self.taxonomy.export()
        return data


class CrowdImageMulticlassSingleBinomial(CrowdImage):
    """ An image to be labeled with a multiclass label.
    """

    def __init__(self, id_, params):
        super(CrowdImageMulticlassSingleBinomial, self).__init__(id_, params)

        self.risk = 1.

    def crowdsource_simple(self, avoid_if_finished=False):
        """Simply do majority vote.
        """
        if avoid_if_finished and self.finished:
            return

        leaf_node_key_set = self.params.leaf_node_key_set
        labels = [anno.label for anno in self.z.values() if anno.label in leaf_node_key_set]
        votes = Counter(labels)
        if len(votes) > 0:
            pred_y = votes.most_common(1)[0][0]

        else:
            # Randomly pick a label.
            # NOTE: could bias with the class priors.
            pred_y = random.choice(self.params.leaf_node_keys)

        self.y = CrowdLabelMulticlassSingleBinomial(
            image=self, worker=None, label=pred_y)


    def predict_true_labels(self, avoid_if_finished=False):
        """ Compute the y that is most likely given the annotations, worker
        skills, etc.
        """

        # NOTE: This is tricky. If we are estimating worker trust, then we
        # actually want to compute the log likelihood of the annotations below.
        # In which case we will return after doing that.
        if avoid_if_finished and self.finished and not self.params.model_worker_trust:
            return

        node_priors = self.params.node_priors
        leaf_node_indices = self.params.leaf_integer_ids
        class_priors = node_priors[leaf_node_indices]

        num_nodes = node_priors.shape[0]
        num_classes = leaf_node_indices.shape[0]

        node_labels = np.arange(num_nodes)

        max_path_length = self.params.taxonomy.max_depth + 1

        # Get the number of workers that have labeled this image
        ncv = self.params.naive_computer_vision
        num_workers = sum([1 for anno in self.z.itervalues() if not anno.is_computer_vision() or ncv])

        # Collect the relevant data from each worker:
        M = np.empty([num_workers, num_nodes, num_nodes], dtype=np.float32)
        N = np.empty([num_workers, num_nodes], dtype=np.float32)
        worker_labels = np.empty(num_workers, dtype=np.intp)
        worker_prob_trust = np.empty(num_workers, dtype=np.float32)

        w = 0
        for anno in self.z.itervalues():
            if not anno.is_computer_vision() or ncv:
                #M[w] = anno.worker.M.toarray()
                wM, wN = anno.worker.build_M_and_N()
                M[w] = wM
                N[w] = wN
                integer_label = self.params.orig_node_key_to_integer_id[anno.label]
                worker_labels[w] = integer_label
                worker_prob_trust[w] = anno.worker.prob_trust
                w += 1


        # Build a tensor that stores the probability of prior annotations given that a worker
        # said "z"
        # p(H^{t-1} | z_j, w_j)
        # Each worker j will represent a row. Each column z will represent
        # a node. Each entry [j, z] will be the probability of the previous
        # annotations given that the worker j provided label z.
        # Use 1s as the default value.
        prob_prior_responses = np.ones((num_workers, num_nodes), dtype=np.float)
        if num_workers > 1:
            previous_label = worker_labels[0]
            worker_label = worker_labels[1]
            pt = worker_prob_trust[1]
            pnt = 1. - pt
            ppnt = (1. - pt) * node_priors[worker_label]
            # Put pt in the spot where the previous label occurs, put ppnt in all other spots
            prob_prior_responses[1] = np.where(node_labels == previous_label, pt, ppnt)

            # Fill in the subsequent rows for each additional worker
            for wind in xrange(2, num_workers):

                previous_label = worker_labels[wind-1]
                worker_label = worker_labels[wind]
                pt = worker_prob_trust[wind]
                pnt = 1. - pt
                ppnt = pnt * node_priors[worker_label]

                # For each possible value of z, we want to compute p(H^{t-1} | z, w_j^t)

                # Numerator : p(z_j^{t-1} | z, w_j^t) * p(H^{t-2} | z_j^{t-1}, w_j^{t-1})
                # Let X = p(H^{t-2} | z_j^{t-1}, w_j^{t-1}), which is fixed
                # The numerator values will be ppnt * X, except for the location
                # where z == previous label, which will have the value pt * X
                ppr = prob_prior_responses[wind - 1][previous_label] # p(H^{t-2} | z_j^{t-1}, w_j^{t-1})
                num = np.full(shape=num_nodes, fill_value=ppnt * ppr) # fill in (1 - p_j)p(z_j) * p(H^{t-2} | z_j, w_j) for all values
                num[previous_label] = pt * ppr # (this is where z == previous_label)

                # Denominator: Sum( p(z | z_j^t, w_j^t) * p(H^{t-2} | z, w_j^{t-1}) )
                # For each possible value of z, we will have 1 location where z == previous_label
                match = pt * prob_prior_responses[wind - 1]
                # For each possible value of z, we will have (num_nodes-1) locations where z != previous label
                no_match = ppnt * prob_prior_responses[wind - 1]
                no_match_sum = no_match.sum()
                # For each possible value of z, add the single match value with all no match values, and subtract off the no match value that is actually a match
                denom = (match + no_match_sum) - no_match

                # p(H^{t-1} | z_j, w_j)
                prob_prior_responses[wind] = num / denom

        # Store these computions with the labels, to be used when computing the log likelihood.
        wind = 0
        for anno in self.z.values():
            if not anno.is_computer_vision() or ncv:
                wl = worker_labels[wind]
                anno.prob_prev_annos = prob_prior_responses[wind, wl]
                wind += 1
        # NOTE: see above (we needed to compute the likelihood of the annotations)
        if avoid_if_finished and self.finished:
            return


        # Compute the likelihood of each possible annotation that a worker could provide
        # Dimensions:
        # 0th: worker index w
        # 1st: class index y
        # 2nd: annotation node index z
        # 3rd: values from either M or N (or 1) along the taxonomic path from root->z
        annotation_probs = np.ones((num_workers, num_classes, num_nodes, max_path_length - 1), dtype=np.float32)

        # We've precomputed, for every path, which values need to be copied to which locations.
        # Copy over the values from M for each worker
        annotation_probs[:,
                        self.params.annotation_probs_insert_from_M[0],
                        self.params.annotation_probs_insert_from_M[1],
                        self.params.annotation_probs_insert_from_M[2]] = M[:, self.params.annotation_probs_M_indices[0], self.params.annotation_probs_M_indices[1]]

        # Copy over the values from N for each worker
        annotation_probs[:,
                        self.params.annotation_probs_insert_from_N[0],
                        self.params.annotation_probs_insert_from_N[1],
                        self.params.annotation_probs_insert_from_N[2]] = N[:, self.params.annotation_probs_N_indices[0]]

        # Take the product along the taxonomic paths to get the probability of each possible annotation.
        # We have to take the product due to the log being outside the sum in the denominator below.
        annotation_probs = np.prod(annotation_probs, axis=3)
        # This gives us a [num_workers, num_classes, num_nodes] tensor corresponding to the probability that
        # a worker w provided annotation z given that the true class is y.

        # We'll transpose so that we have classes first
        annotation_probs = np.transpose(annotation_probs, axes=(1, 0, 2))
        # This gives us a [num_classes, num_workers, num_nodes] tensor


        # Now we want to compute p(y | Z) for each possible value of y
        # p(y | Z) = p(y) * Prod( p(z| y, H, w) )
        # = [ p(z| y, w) * p(H^t-1 | z, w) ] / [ Sum( p(z| y, w) * p(H^t-1 | z, w) ) ]
        # We'll be using logs, so the multiplication can be a sum and the division can be a subtraction.

        # Numerator
        # We'll use integer indexing, hence the use of np.arange(num_workers)
        # [num_classes, num_workers] -> [num_classes, num_workers]
        widx = np.arange(num_workers)
        num = np.log(annotation_probs[:, widx, worker_labels]) + np.log(prob_prior_responses[widx, worker_labels])
        # Denominator [num_clases, num_workers, num_nodes] -> [num_classes, num_workers]
        denom = np.sum(annotation_probs[:] * prob_prior_responses, axis=2)
        # Division
        # [num_classes, num_workers] -> [num_classes]
        prob_of_annos = np.sum(num - np.log(denom), axis=1)

        # Tack on the class priors
        class_log_likelihoods = prob_of_annos + np.log(class_priors)

        # Get the most likely prediction
        arg_max_index = np.argmax(class_log_likelihoods)
        pred_y_integer_id = leaf_node_indices[arg_max_index]

        pred_y = self.params.integer_id_to_orig_node_key[pred_y_integer_id]
        self.y = CrowdLabelMulticlassSingleBinomial(image=self, worker=None, label=pred_y)

        # Compute the risk of the predicted label
        # Subtract the maximum value for numerical stability
        m = class_log_likelihoods[arg_max_index]
        num = 1.
        denom = np.sum(np.exp(class_log_likelihoods - m))
        prob_y = num / denom
        self.risk = 1. - prob_y

        if self.id == '1112246':
            print(num_nodes)
            print(num_classes)
            print(worker_labels)
            s = np.argsort(class_log_likelihoods)[::-1][:10]
            labels = leaf_node_indices[s]
            keys = [self.params.integer_id_to_orig_node_key[i] for i in labels]
            print(s)
            print(labels)
            print(keys)
            print(class_log_likelihoods[s])
            #print(class_log_likelihoods)
            print(np.log(class_priors)[s])
            print(prob_of_annos[s])
            #print(annotation_probs[:, widx, worker_labels])
            print(annotation_probs[33, widx, worker_labels])
            print(prob_prior_responses[widx, worker_labels])
            print(arg_max_index)
            print(pred_y_integer_id)
            print(pred_y)
            print(prob_y)

            for w, anno in enumerate(self.z.itervalues()):
                 print(anno.worker.id)
                 print(anno.worker.skill)
                 print(M[w, 60, 60])


    def compute_log_likelihood(self):
        """Compute the log likelihood of the predicted label given the prior
        that the class is present.
        """
        y = self.y.label

        if self.cv_pred != None:
            ll = math.log(self.cv_pred.prob[y])
        else:
            ll = math.log(self.params.class_probs[y])

        return ll

    def estimate_parameters(self, avoid_if_finished=False):
        """We didn't bother with the image difficulty parameters for this task.
        """
        return

    def check_finished(self, set_finished=True):
        """ Set finish if our risk is less than the threshold.
        """
        if self.finished:
            return True

        finished = self.risk <= self.params.min_risk
        if set_finished:
            self.finished = finished

        return finished


class CrowdWorkerMulticlassSingleBinomial(CrowdWorker):
    """ A worker providing multiclass labels.
    """
    def __init__(self, id_, params):
        super(CrowdWorkerMulticlassSingleBinomial, self).__init__(id_, params)

        # Placeholder for generic skill.
        self.prob = None
        self.skill = None

        # Copy over the global probabilities
        self.taxonomy = None
        self.encode_exclude['taxonomy'] = True

        self.prob_trust = params.prob_trust
        self._rec_cache = {}
        self.encode_exclude['_rec_cache'] = True

        self.skill_vector = None
        self.M = None
        self.N = None
        self.encode_exclude['M'] = True
        self.encode_exclude['N'] = True


    def compute_log_likelihood(self):
        """ The log likelihood of the skill.
        """
        ll = 0

        pooled_prob_correct = self.params.pooled_prob_correct_vector
        prob_correct = self.skill_vector

        ll = np.sum(
            (pooled_prob_correct * self.params.prob_correct_beta - 1) * np.log(prob_correct) +
            (( 1. - pooled_prob_correct) * self.params.prob_correct_beta - 1) * np.log( 1. - prob_correct)
        )

        if self.params.model_worker_trust:
            ll += ((self.params.prob_trust * self.params.prob_trust_beta - 1) * math.log(self.prob_trust) +
                   ((1 - self.params.prob_trust) * self.params.prob_trust_beta - 1) * math.log(1. - self.prob_trust))

        return ll


    def estimate_parameters(self, avoid_if_finished=False):
        """ Estimate the worker skill parameters.
        """

        #assert self.taxonomy is not None, "Worker %s's taxonomy was not initialized"

        # For the single binomial taxonomic model, we are learning a single parameter
        # at each internal node of the taxonomy.
        skill_counts_num = np.zeros_like(self.params.pooled_prob_correct_vector)
        skill_counts_denom = np.zeros_like(self.params.pooled_prob_correct_vector)

        for image in self.images.itervalues():

            if len(image.z) <= 1:
                continue

            y_integer_id = self.params.orig_node_key_to_integer_id[image.y.label]
            z_integer_id = self.params.orig_node_key_to_integer_id[image.z[self.id].label]

            y_node_list = self.params.root_to_node_path_list[y_integer_id]
            z_node_list = self.params.root_to_node_path_list[z_integer_id]

            # update the denominator
            skill_counts_denom[y_node_list[:-1]] += 1

            # Traverse the paths and update the numerator when the nodes match
            # Note that we update the parent count when the children match
            for child_node_index in range(1, len(y_node_list)):
                y_parent_node = y_node_list[child_node_index - 1]
                if child_node_index < len(z_node_list):
                    y_node = y_node_list[child_node_index]
                    z_node = z_node_list[child_node_index]
                    if y_node == z_node:
                        skill_counts_num[y_parent_node] += 1
                else:
                    # we've exhausted the z node list
                    break

        num = self.params.prob_correct_beta * self.params.pooled_prob_correct_vector + skill_counts_num
        denom = self.params.prob_correct_beta + skill_counts_denom
        denom = np.clip(denom, a_min=0.00000001, a_max=None)
        self.skill_vector = np.clip(num / denom, a_min=0.00000001, a_max=0.99999)
        #self.build_M_and_N()

        # Placeholder for skills
        total_num_correct = 0.
        for image in self.images.itervalues():
            y = image.y.label
            z = image.z[self.id].label
            if y == z:
                total_num_correct += 1.

        prob = total_num_correct / max(0.0001, len(self.images))
        self.prob = prob
        self.skill = [self.prob]

        # Estimate our probability of trusting previous annotations by looking
        # at our agreement with previous annotations
        if self.params.model_worker_trust:
            prob_trust_num = self.params.prob_trust_beta * self.params.prob_trust
            prob_trust_denom = self.params.prob_trust_beta

            for image in self.images.itervalues():

                if self.params.recursive_trust:
                    # We are only dependent on the annotation immediately
                    # before us.
                    our_t = image.z.keys().index(self.id)
                    if our_t > 0:
                        our_label = image.z[self.id].label
                        prev_anno = image.z.values()[our_t - 1]

                        prob_trust_denom += 1.
                        if our_label == prev_anno.label:
                            prob_trust_num += 1.
                else:
                    # We treat each previous label independently
                    our_label = image.z[self.id].label
                    for prev_worker_id, prev_anno in image.z.iteritems():
                        if prev_worker_id == self.id:
                            break
                        if not prev_anno.is_computer_vision() or self.params.naive_computer_vision:
                            prob_trust_denom += 1.
                            if our_label == prev_anno.label:
                                prob_trust_num += 1.

            self.prob_trust = np.clip(
                prob_trust_num / float(prob_trust_denom), 0.00000001, 0.9999)
            self.skill.append(self.prob_trust)

    def build_M_and_N(self):

        # Build the M matrix.
        # The size will be [num nodes, num nodes]
        # M[y, z] is the probability of predicting class z when the true class is y.
        num_nodes = self.params.node_priors.shape[0]
        M = np.zeros([num_nodes, num_nodes], dtype=np.float32)

        # Fill in the diagonals
        M[self.params.M_correct_rows, self.params.M_correct_cols] = self.skill_vector[self.params.skill_vector_correct_read_indices]

        # Fill in the off diagonals
        pnc = 1 - self.skill_vector[self.params.skill_vector_incorrect_read_indices]
        ppnc = pnc * self.params.node_priors[self.params.skill_vector_node_priors_read_indices]
        M[self.params.M_incorrect_rows, self.params.M_incorrect_cols] = ppnc

        # Build the M matrix.
        # The size will be [num nodes, num nodes]
        # M[y, z] is the probability of predicting class z when the true class is y.
        # num_nodes = self.params.node_priors.shape[0]
        # M = np.zeros([num_nodes, num_nodes], dtype=np.float32)
        # for node_indices in self.params.parent_and_siblings_indices:
        #     parent_index = node_indices[0]
        #     sibling_indices = node_indices[1:]
        #     num_siblings = len(sibling_indices)

        #     # Probability of correct and not correct
        #     pc = self.skill_vector[parent_index]
        #     pnc = 1 - pc

        #     # Multiply the probability of not correct by the prior on the node
        #     ppnc = self.params.node_priors[sibling_indices] * pnc

        #     # M[y,z] = the probability of predicting class z when the true class is y.
        #     # Set the off diagonal entries to the probability of not being correct times the node prior
        #     M[np.ix_(sibling_indices, sibling_indices)] = np.tile(ppnc, [num_siblings, 1])
        #     # Set the diagonal to the probability of being correct
        #     M[sibling_indices, sibling_indices] = pc
        #self.M = M

        # blks = [np.array([1])] # Store a skill value of 1 for the root node (we do this to keep the indexing constant)
        # for node_indices in self.params.parent_and_siblings_indices:
        #     parent_index = node_indices[0]
        #     sibling_indices = node_indices[1:]
        #     num_siblings = len(sibling_indices)

        #     # Probability of correct and not correct
        #     pc = self.skill_vector[parent_index]
        #     pnc = 1 - pc

        #     # Multiply the probability of not correct by the prior on the node
        #     ppnc = self.params.node_priors[sibling_indices] * pnc

        #     # Geneate the block matrix, first by tiling the probability of not correct
        #     blk = np.tile(ppnc, [num_siblings, 1])
        #     # then fill in the diagonal with the probability of being correct
        #     np.fill_diagonal(blk, pc)
        #     blks.append(blk)
        # self.M = block_diag(*blks)
        #self.M = sparse_block_diag(blks)

        # Build the N vector. This is the probability of a worker selecting a node regardless
        # of the true value (i.e. when the parent nodes are different)
        num_nodes = len(self.params.taxonomy.nodes)
        N = np.ones([num_nodes], dtype=np.float32) # This will store a value of 1 for the root node. We do this to keep the indexing constant

        # Get the probability of not correct
        pc = self.skill_vector[self.params.parent_indices]
        pnc = 1 - pc

        # Multiply the probability of not correct by the prior on the node
        ppnc = self.params.node_priors[1:] * pnc
        N[1:] = ppnc # don't overwrite the root node.

        return M, N

    def parse(self, data):
        super(CrowdWorkerMulticlassSingleBinomial, self).parse(data)
        if 'skill_vector' in data:
            self.skill_vector = np.array(data['skill_vector'])
            #self.build_M_and_N()
        #if 'taxonomy_data' in data:
        #    self.taxonomy = Taxonomy()
        #    self.taxonomy.load(data['taxonomy_data'])
        #    self.taxonomy.finalize()

    def encode(self):
        data = super(CrowdWorkerMulticlassSingleBinomial, self).encode()
        if self.skill_vector is not None:
            data['skill_vector'] = self.skill_vector.tolist()
        #if self.taxonomy is not None:
        #    data['taxonomy_data'] = self.taxonomy.export()
        return data


class CrowdLabelMulticlassSingleBinomial(CrowdLabel):
    """ A multiclass label.
    """

    def __init__(self, image, worker, label=None):
        super(CrowdLabelMulticlassSingleBinomial, self).__init__(image, worker)

        self.label = label
        self.gtype = 'multiclass_single_bin'
        self.prob_prev_annos = None

    def compute_log_likelihood(self):
        """ The likelihood of the label.
        """
        prob_correct = self.worker.skill_vector
        node_probs = self.worker.params.node_priors

        y = self.image.y.label
        z = self.label

        y_integer_id = self.worker.params.orig_node_key_to_integer_id[y]
        z_integer_id = self.worker.params.orig_node_key_to_integer_id[z]

        y_node_list = self.worker.params.root_to_node_path_list[y_integer_id]
        z_node_list = self.worker.params.root_to_node_path_list[z_integer_id]

        ll = 0.
        if len(z_node_list) > 1:

            # pad the y_node_list
            pad_size = max(0, len(z_node_list) - len(y_node_list))
            y_node_list = y_node_list + [-1] * pad_size

            for i in range(len(z_node_list) - 1):
                # Same parents
                if y_node_list[i] == z_node_list[i]:
                    # Same node
                    if y_node_list[i+1] == z_node_list[i+1]:
                        ll += math.log(prob_correct[y_node_list[i]])
                    # Different node
                    else:
                        ll += math.log(1 - prob_correct[y_node_list[i]]) + math.log(node_probs[z_node_list[i+1]])
                # Different parents
                else:
                    ll += math.log(1 - prob_correct[z_node_list[i]]) + math.log(node_probs[z_node_list[i+1]])

        if self.worker.params.model_worker_trust:
            # Should have been computed when estimating the labels
            assert self.prob_prev_annos is not None
            ll += math.log(self.prob_prev_annos)

        return ll

    def loss(self, y):
        return 1. - float(self.label == y.label)

    def parse(self, data):
        super(CrowdLabelMulticlassSingleBinomial, self).parse(data)
        self.label = self.label