from joblib import Memory
from .images_fast import index_array
from modl.datasets import get_data_dirs
from os.path import join

from nilearn._utils import CacheMixin
from scipy.misc import face
from skimage.io import imread
from skimage.transform import rescale
from sklearn.feature_extraction.image import extract_patches
from sklearn.utils import check_random_state, gen_batches
from spectral import open_image

import numpy as np


def load_images(source,
                scale=1,
                gray=False,
                memory=Memory(cachedir=None)):
    data_dir = get_data_dirs()[0]
    if source == 'face':
        image = face(gray=gray)
        image = image / 255
        if image.ndim == 2:
            image = image[..., np.newaxis]
        if scale != 1:
            image = memory.cache(rescale)(image, scale=scale)
        return image
    elif source == 'lisboa':
        image = imread(join(data_dir, 'images', 'lisboa.jpg'), as_grey=gray)
        image = image / 255
        if image.ndim == 2:
            image = image[..., np.newaxis]
        if scale != 1:
            image = memory.cache(rescale)(image, scale=scale)
        return image
    elif source == 'aviris':
        image = open_image(
            join(data_dir,
                 'aviris',
                 'f100826t01p00r05rdn_b/'
                 'f100826t01p00r05rdn_b_sc01_ort_img.hdr'))
        image = np.array(image.open_memmap()).astype('f8')
        image[image == -50] = -1
        image [image != -50] /= 65535
        return image
    else:
        raise ValueError('Data source is not known')

class Batcher(object):
    def __init__(self, patch_shape=(8,), batch_size=10, random_state=None,
                 clean=True,
                 max_samples=None):
        self.patch_shape = patch_shape
        self.batch_size = batch_size
        self.random_state = random_state
        self.clean = clean
        self.max_samples = max_samples

    def prepare(self, image):
        if len(self.patch_shape) == 1:
            self.patch_shape_ = (self.patch_shape, self.patch_shape)
        if len(self.patch_shape) == 2:
            self.patch_shape_ = (self.patch_shape[0],
                                 self.patch_shape[1], image.shape[2])
        self.patches_ = extract_patches(image, patch_shape=self.patch_shape_)
        self.random_state_ = check_random_state(self.random_state)
        self.patch_indices_ = index_array(self.patches_,
                                          max_samples=self.max_samples
                                          if self.max_samples is not None
                                          else -1,
                                          clean=self.clean,
                                          random_seed=0)
        self.n_samples_ = self.patch_indices_.shape[0]

        self.sample_indices_ = np.arange(self.n_samples_, dtype='i4')

        self.cast_ = image.dtype == np.dtype('>i2')

    def generate(self, n_epochs=1):
        for _ in range(n_epochs):
            batches = gen_batches(self.n_samples_, self.batch_size)
            permutation = self.random_state_.permutation(self.n_samples_)
            self.sample_indices_ = self.sample_indices_[permutation]
            self.patch_indices_ = self.patch_indices_[permutation]
            for batch in batches:
                if self.cast_:
                    yield self.patches_[list(self.patch_indices_[batch].T)].\
                              astype('float64') / 65535, \
                          self.sample_indices_[batch]
                else:
                    yield self.patches_[list(self.patch_indices_[batch].T)], \
                          self.sample_indices_[batch]

    def generate_one(self):
        return next(self.generate())