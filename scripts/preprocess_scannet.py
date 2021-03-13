""" Preprocess ScanNet dataset and cache in Numpy data files """
import os
from pathlib import Path
from os.path import join, isfile
import concurrent.futures
import argparse
import json
import csv
import logging as log
import traceback
import yaml
import numpy as np
from tqdm import tqdm
import open3d as o3d
from open3d.ml.datasets import utils


def parse_args():
    parser = argparse.ArgumentParser(description='Preprocess ScanNet Dataset.')
    parser.add_argument('--dataset_path',
                        help='path to ScanNet scans directory',
                        required=True)
    parser.add_argument('--out_path',
                        help='Output path to store processed data.',
                        default=None,
                        required=False)
    parser.add_argument(
        '--only_stats',
        help='Do not preprocess. Only compute dataset statistics.',
        default=False,
        action='store_true',
        required=False)

    args = parser.parse_args()

    dict_args = vars(args)
    for k in dict_args:
        v = dict_args[k]
        print("{}: {}".format(k, v) if v is not None else "{} not given".
              format(k))

    return args


def represents_int(s):
    """Judge whether string s represents an int.
    Args:
        s(str): The input string to be judged.
    Returns:
        bool: Whether s represents int or not.
    """
    try:
        int(s)
        return True
    except ValueError:
        return False


class ScannetProcess():
    """Preprocess ScanNet.
    This class converts ScanNet raw data into npy files.
    Args:
        dataset_path (str): Directory to load ScanNet data.
        out_path (str): Directory to save pickle file(infos).
    """

    def __init__(self, dataset_path, out_path, max_num_point=100000):

        self.out_path = out_path
        self.dataset_path = dataset_path
        self.max_num_point = max_num_point

        scans = os.listdir(dataset_path)
        self.scans = []
        for scan in scans:
            name = scan.split('/')[-1]
            if 'scene' in name and len(name) == 12:
                self.scans.append(scan)

        self.DONOTCARE_IDS = np.array([])
        self.OBJ_CLASS_IDS = np.array(
            [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16, 24, 28, 33, 34, 36, 39])

        print(f"Total number of scans : {len(self.scans)}")

    def convert(self):
        errors = []

        # with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        #     future_scene_stats = [
        #         executor.submit(self.process_scene, scan) for scan in self.scans
        #     ]
        #     for future in tqdm(future_scene_stats):
        for scan in tqdm(self.scans):
            try:
                self.process_scene(scan)
            except Exception as e:
                errors.append(f'{scan}: ' + traceback.format_exc())

            # executor.shutdown(wait=True)

        if errors:
            errmsg = "Processing failed:\n" + "\n".join(errors)
            log.warning(errmsg)
        with open(join(self.out_path, 'errors.txt'), 'w') as errfile:
            errfile.write(errmsg)

    def process_scene(self, scan):
        if (isfile(f'{join(self.out_path, scan)}_vert.npy') and
                isfile(f'{join(self.out_path, scan)}_sem_label.npy') and
                isfile(f'{join(self.out_path, scan)}_ins_label.npy') and
                isfile(f'{join(self.out_path, scan)}_bbox.npy')):
            return

        in_path = join(self.dataset_path, scan)
        mesh_file = join(in_path, scan + '_vh_clean_2.ply')
        agg_file = join(in_path, scan + '.aggregation.json')
        seg_file = join(in_path, scan + '_vh_clean_2.0.010000.segs.json')

        meta_file = join(in_path, scan + '.txt')
        label_map_file = str(
            Path(__file__).parent /
            '../ml3d/datasets/_resources/scannet/scannetv2-labels.combined.tsv')
        (mesh_vertices, semantic_labels, instance_labels, instance_bboxes,
         instance2semantic) = self.export(mesh_file, agg_file, seg_file,
                                          meta_file, label_map_file)

        mask = np.logical_not(np.in1d(semantic_labels, self.DONOTCARE_IDS))
        mesh_vertices = mesh_vertices[mask, :]
        semantic_labels = semantic_labels[mask]
        instance_labels = instance_labels[mask]

        num_instances = len(np.unique(instance_labels))
        print(f'Num of instances: {num_instances}')

        bbox_mask = np.in1d(instance_bboxes[:, -1], self.OBJ_CLASS_IDS)
        instance_bboxes = instance_bboxes[bbox_mask, :]
        print(f'Num of care instances: {instance_bboxes.shape[0]}')

        N = mesh_vertices.shape[0]
        if N > self.max_num_point:
            choices = np.random.choice(N, self.max_num_point, replace=False)
            mesh_vertices = mesh_vertices[choices, :]
            semantic_labels = semantic_labels[choices]
            instance_labels = instance_labels[choices]

        np.save(f'{join(self.out_path, scan)}_vert.npy', mesh_vertices)
        np.save(f'{join(self.out_path, scan)}_sem_label.npy', semantic_labels)
        np.save(f'{join(self.out_path, scan)}_ins_label.npy', instance_labels)
        np.save(f'{join(self.out_path, scan)}_bbox.npy', instance_bboxes)

    def export(self, mesh_file, agg_file, seg_file, meta_file, label_map_file):
        mesh_vertices = self.read_mesh_vertices_rgb(mesh_file)
        label_map = self.read_label_mapping(label_map_file,
                                            label_from='raw_category',
                                            label_to='nyu40id')

        # Load axis alignment matrix
        lines = open(meta_file).readlines()
        for line in lines:
            if 'axisAlignment' in line:
                axis_align_matrix = [
                    float(x)
                    for x in line.rstrip().strip('axisAlignment = ').split(' ')
                ]
                break
        axis_align_matrix = np.array(axis_align_matrix).reshape((4, 4))
        pts = np.ones((mesh_vertices.shape[0], 4))
        pts[:, 0:3] = mesh_vertices[:, 0:3]
        pts = np.dot(pts, axis_align_matrix.transpose())
        mesh_vertices[:, 0:3] = pts[:, 0:3]

        # Load instance and semantic labels.
        object_id_to_segs, label_to_segs = self.read_aggregation(agg_file)
        seg_to_verts, num_verts = self.read_segmentation(seg_file)

        label_ids = np.zeros(shape=(num_verts), dtype=np.uint32)
        object_id_to_label_id = {}
        for label, segs in label_to_segs.items():
            label_id = label_map[label]
            for seg in segs:
                verts = seg_to_verts[seg]
                label_ids[verts] = label_id

        instance_ids = np.zeros(shape=(num_verts),
                                dtype=np.uint32)  # 0: unannotated
        num_instances = len(np.unique(list(object_id_to_segs.keys())))
        for object_id, segs in object_id_to_segs.items():
            for seg in segs:
                verts = seg_to_verts[seg]
                instance_ids[verts] = object_id
                if object_id not in object_id_to_label_id:
                    object_id_to_label_id[object_id] = label_ids[verts][0]

        instance_bboxes = np.zeros((num_instances, 7))
        for obj_id in object_id_to_segs:
            label_id = object_id_to_label_id[obj_id]
            obj_pc = mesh_vertices[instance_ids == obj_id, 0:3]
            if len(obj_pc) == 0:
                continue
            xmin = np.min(obj_pc[:, 0])
            ymin = np.min(obj_pc[:, 1])
            zmin = np.min(obj_pc[:, 2])
            xmax = np.max(obj_pc[:, 0])
            ymax = np.max(obj_pc[:, 1])
            zmax = np.max(obj_pc[:, 2])
            bbox = np.array([(xmin + xmax) / 2, (ymin + ymax) / 2,
                             (zmin + zmax) / 2, xmax - xmin, ymax - ymin,
                             zmax - zmin, label_id])
            # NOTE: this assumes obj_id is in 1,2,3,.,,,.NUM_INSTANCES
            instance_bboxes[obj_id - 1, :] = bbox

        return mesh_vertices, label_ids, instance_ids,\
            instance_bboxes, object_id_to_label_id

    @staticmethod
    def read_label_mapping(filename,
                           label_from='raw_category',
                           label_to='nyu40id'):
        assert os.path.isfile(filename)
        mapping = dict()
        with open(filename) as csvfile:
            reader = csv.DictReader(csvfile, delimiter='\t')
            for row in reader:
                mapping[row[label_from]] = int(row[label_to])
        if represents_int(list(mapping.keys())[0]):
            mapping = {int(k): v for k, v in mapping.items()}
        return mapping

    @staticmethod
    def read_mesh_vertices_rgb(filename):
        """Read XYZ and RGB for each vertex.
        Args:
            filename(str): The name of the mesh vertices file.
        Returns:
            Vertices. Note that RGB values are in 0-255.
        """
        assert os.path.isfile(filename)
        with open(filename, 'rb') as f:
            data = o3d.t.io.read_point_cloud(f.name).point
            points = data["points"].numpy().astype(np.float32)
            colors = data["colors"].numpy().astype(np.float32)
            vertices = np.concatenate([points, colors], axis=1)
        return vertices

    @staticmethod
    def read_aggregation(filename):
        assert os.path.isfile(filename)
        object_id_to_segs = {}
        label_to_segs = {}
        with open(filename) as f:
            data = json.load(f)
            num_objects = len(data['segGroups'])
            for i in range(num_objects):
                object_id = data['segGroups'][i][
                    'objectId'] + 1  # instance ids should be 1-indexed
                label = data['segGroups'][i]['label']
                segs = data['segGroups'][i]['segments']
                object_id_to_segs[object_id] = segs
                if label in label_to_segs:
                    label_to_segs[label].extend(segs)
                else:
                    label_to_segs[label] = segs
        return object_id_to_segs, label_to_segs

    @staticmethod
    def read_segmentation(filename):
        assert os.path.isfile(filename)
        seg_to_verts = {}
        with open(filename) as f:
            data = json.load(f)
            num_verts = len(data['segIndices'])
            for i in range(num_verts):
                seg_id = data['segIndices'][i]
                if seg_id in seg_to_verts:
                    seg_to_verts[seg_id].append(i)
                else:
                    seg_to_verts[seg_id] = [i]
        return seg_to_verts, num_verts

    def compute_dataset_statistics(self):

        def get_scene_stats(scan):
            mesh_vertices = np.load(f'{join(self.out_path, scan)}_vert.npy')
            semantic_labels = np.load(
                f'{join(self.out_path, scan)}_sem_label.npy')
            instance_labels = np.load(
                f'{join(self.out_path, scan)}_ins_label.npy')
            instance_bboxes = np.load(f'{join(self.out_path, scan)}_bbox.npy')
            return utils.statistics.compute_scene_stats(mesh_vertices,
                                                        semantic_labels,
                                                        instance_labels,
                                                        instance_bboxes)

        # ProcessPoolExecutor deadlocks
        with concurrent.futures.ThreadPoolExecutor() as executor:
            scene_stats = executor.map(get_scene_stats, self.scans)

        dataset_stats = utils.statistics.compute_dataset_stats(
            list(scene_stats))
        with open(join(self.out_path, 'summary.yaml'), 'w') as sumfile:
            yaml.dump(dataset_stats, sumfile)


if __name__ == '__main__':
    args = parse_args()
    if args.out_path is None:
        args.out_path = args.dataset_path
    converter = ScannetProcess(args.dataset_path, args.out_path)
    if not args.only_stats:
        converter.convert()
    converter.compute_dataset_statistics()