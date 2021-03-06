from sklearn.exceptions import NotFittedError
import pandas as pd
import numpy as np
from pandas.api.types import is_numeric_dtype, is_number
import bisect
import functools
from collections import defaultdict
from typing import Dict, Iterable, Tuple, List

from ..utils import force_zero_one, make_series, searchsorted
from .base import Binning
from .unsupervised import equal_frequency_binning

from .tree import TreeBinner, tree_binning


__all__ = ['ChiSquareBinning', 'KSBinning', 'EntropyBinning', 'IVBinning', 'TreeBinner']


def sorted_two_gram(X):
    """ Two gram with the left element smaller than the right element in each pair
        eg. sorted_two_gram([1, 3, 2]) -> [(1, 2), (2, 3)]
    """
    unique_values = sorted(X)
    return [(unique_values[i], unique_values[i + 1])
            for i in range(len(unique_values) - 1)]


def is_monotonic(i, strict=True, ignore_na=True) -> bool:
    """ Check if an iterable is monotonic """
    i = make_series(i)
    diff = i.diff()[1:]
    if ignore_na:
        diff = diff[diff.notnull()]
    sign = diff > 0 if strict else diff >= 0
    if sign.sum() == 0 or (~sign).sum() == 0:
        return True
    return False



class CumulativeCounter:
    """ Keep tracks of the cumulative number of samples and positive samples """
    
    def __init__(self, X: pd.Series, y: pd.Series, bins=None):
        self.name = X.name
        self.total_sample_with_null = len(X)
        self.total_sample = X.notnull().sum()
        self.total_pos = y[X.notnull()].sum()
        self.total_neg = self.total_sample - self.total_pos
        
        self.mapping = dict()
        if bins is None:
            values = X[X.notnull()].unique()
        else:
            values = pd.qcut(X, q=bins, duplicates='drop', retbins=True)[1]
            
        for v in values:
            # the last interval should be right closed
            smaller = X < v if v != X.max() else X <= v
            n_sample = smaller.sum()
            n_pos = y[smaller].sum()
            self.mapping[v] = (n_sample, n_pos)
            
    def __getitem__(self, key):
        return self.mapping[key]

    def __eq__(self, other):
        return self.name == other.name

    def __hash__(self):
        return hash(self.name)
            
    def keys(self):
        return sorted(self.mapping.keys())
    
    def iter_keys(self, drop_first=True, drop_last=True):
        start = 0 + int(drop_first)
        end = len(self.mapping) - int(drop_last)
        for key in self.keys()[start:end]:
            yield key
    
    def iter_neighbour_keys(self):
        return sorted_two_gram(self.mapping.keys())
    
    def pct_pos_lt_key(self, key):
        """ Percentage of positive samples when less than given key """
        n, n_pos = self[key]
        return n_pos / n
    
    def pct_pos_ge_key(self, key):
        """ Percentage of positive samples when larger or equal than given key """
        n, n_pos = self[key]
        n, n_pos = self.total_sample - n, self.total_pos - n_pos
        return n_pos / n
    
    def pct_pos_between_keys(self, k1, k2):
        """ Percentage of positive samples when k1 <= key < k2. """
        n_1, n_pos_1 = self[k1]
        n_2, n_pos_2 = self[k2]
        return (n_pos_2 - n_pos_1) / (n_2 - n_1)

    def n_sample_between_keys(self, k1, k2):
        """ Number of samples when k1 <= key < k2 """
        n_1, _ = self[k1]
        n_2, _ = self[k2]
        return n_2 - n_1

    def n_positive_sample_between_keys(self, k1, k2):
        _, n_pos_1 = self[k1]
        _, n_pos_2 = self[k2]
        return n_pos_2 - n_pos_1

    def entropy_between_keys(self, k1, k2):
        pct_pos = self.pct_pos_between_keys(k1, k2)
        pct_neg = 1 - pct_pos
        return -np.sum([p * np.log2(p) if p > 0 else 0 for p in [pct_pos, pct_neg]])

    def woe_between_keys(self, k1, k2):
        n_1, n_pos_1 = self[k1]
        n_2, n_pos_2 = self[k2]

        n_sample, n_pos = n_2 - n_1, n_pos_2 - n_pos_1
        pct_pos = n_pos / self.total_pos
        pct_neg = (n_sample - n_pos) / self.total_neg
        pct_pos, pct_neg = np.clip([pct_pos, pct_neg], a_min=1e-10, a_max=1 - 1e-10)
        return np.log(pct_pos / pct_neg)

    def iv_between_keys(self, k1, k2):
        n_1, n_pos_1 = self[k1]
        n_2, n_pos_2 = self[k2]

        n_sample, n_pos = n_2 - n_1, n_pos_2 - n_pos_1
        pct_pos = n_pos / self.total_pos
        pct_neg = (n_sample - n_pos) / self.total_neg
        pct_pos, pct_neg = np.clip([pct_pos, pct_neg], a_min=1e-10, a_max=1 - 1e-10)
        return (pct_pos - pct_neg) * np.log(pct_pos / pct_neg)
    
    def pct_pos_given_cutoffs(self, cutoffs, include_edge=True):
        """ Return the positive percentage within each interval """
        if include_edge:
            cutoffs = set([min(self.keys())] + cutoffs + [max(self.keys())])
        return np.array([self.pct_pos_between_keys(k1, k2) \
                    for k1, k2 in sorted_two_gram(cutoffs)])
    
    def n_sample_given_cutoffs(self, cutoffs, include_edge=True):
        """ Return number of samples within each interval """
        if include_edge:
            cutoffs = set([min(self.keys())] + cutoffs + [max(self.keys())])
        return np.array([self.n_sample_between_keys(k1, k2) \
                    for k1, k2 in sorted_two_gram(cutoffs)])

    def entropy_given_cutoffs(self, cutoffs):
        """ Return the weighted sum of entropy for all the intervals """
        weight = np.array(self.n_sample_given_cutoffs(cutoffs))
        weight = weight / weight.sum()

        cutoffs = [min(self.keys())] + cutoffs + [max(self.keys())]
        bin_entropy = np.array([self.entropy_between_keys(k1, k2) \
                            for k1, k2 in sorted_two_gram(cutoffs)])
        return (weight * bin_entropy).sum()

    def iv_given_cutoffs(self, cutoffs):
        cutoffs = [min(self.keys())] + cutoffs + [max(self.keys())]
        return sum([self.iv_between_keys(k1, k2) \
                    for k1, k2 in sorted_two_gram(cutoffs)])



class SupervisedBinning(Binning):

    def __init__(self,
                 cols: list = None,
                 bins: dict = None,
                 categorical_cols: list = None,
                 encode: bool = True,
                 fill: int = -1):
        super().__init__(cols, bins, encode, fill)

        self.categorical_cols = categorical_cols
        # mapping for discrete variables
        self.discrete_encoding = dict()

    def encode_with_label(self, X: pd.Series, y: pd.Series) -> pd.Series:
        """ Encode categorical features with its percentage of positive samples"""
        X, y = make_series(X), make_series(y)
        pct_pos = y.groupby(X).mean()
        # save the mapping for transform()
        self.discrete_encoding[X.name] = pct_pos
        return X.map(pct_pos)

    def _transform(self, X: pd.Series, y=None):
        """ Transform a single feature"""
        # map discrete value to its corresponding percentage of positive samples
        col_name = X.name
        if col_name in self.discrete_encoding:
            # if the a new category is encountered, leave it as missing
            X = X.map(self.discrete_encoding[col_name])
        return super()._transform(X, y)

    def get_interval_mapping(self, col_name: str):
        """ Get the mapping from encoded value to its corresponding group. """
        if self.bins is None:
            raise NotFittedError('This {} is not fitted. Call the fit method first.'.format(self.__class__.__name__))

        if col_name in self.discrete_encoding and isinstance(self.bins[col_name], list):
            # categorical columns
            encoding = self.discrete_encoding[col_name]
            group = defaultdict(list)
            for i, v in zip(searchsorted(self.bins[col_name], encoding), encoding.index):
                group[i].append(v)
            group = {k: '[' + ', '.join(map(str, v)) + ']' for k, v in group.items()}
            group[0] = 'UNSEEN'
            return group
        else:
            return super().get_interval_mapping(col_name)

    def get_bin_stats(self, X: pd.Series, y):
        """ Improve formatting by sorting the intervals """
        col = X.name

        if col in self.categorical_cols:
            return super().get_bin_stats(X, y, sort=False)
        else:
            return super().get_bin_stats(X, y, sort=True)



class TopDownBinning(SupervisedBinning):
    """ Base class for supervised binning via top down splits """
    
    # a function to evaluate the cutoff
    # it should take (cumulative counter, candidate cutoff, original cutoff)
    # and return a metric that should be 
    evaluate_cutoff = None

    def __init__(self,
                max_bin: int,
                cols: list=None,
                bins: dict=None,
                categorical_cols: list = None,
                bin_cat_cols: bool = True,
                encode: bool = True,
                fill: int = -1,
                force_monotonic: bool = True,
                force_mix_label: bool = True,
                min_interval_size: float = 0.02,
                prebin: int = 100):
        super().__init__(cols, bins, categorical_cols, encode, fill)
        self.max_bin = max_bin
        self.bin_cat_cols = bin_cat_cols
        self.force_monotonic = force_monotonic
        self.force_mix_label = force_mix_label
        self.min_interval_size = min_interval_size
        self.prebin = prebin

    def _find_single_split(self, counter: CumulativeCounter, cutoffs: list) -> Tuple[list, bool]:     
        next_split, best_metric = None, -1
        for v in counter.iter_keys(drop_first=True, drop_last=True):
            # already in the cutoff
            if v in cutoffs:
                continue

            # make a copy
            candidate_cutoffs = cutoffs[:]
            bisect.insort(candidate_cutoffs, v)

            # check the minimum samples within each bin
            # min_sample_per_bin = int(self.min_interval_size * counter.total_sample)
            min_sample_per_bin = int(self.min_interval_size * counter.total_sample_with_null)

            # early stop when min_interval_size == 0
            if min_sample_per_bin > 0 and any(i < min_sample_per_bin \
                                    for i in counter.n_sample_given_cutoffs(candidate_cutoffs)):
                continue

            # check the positive percentage within each bin
            pct_pos_bin = counter.pct_pos_given_cutoffs(candidate_cutoffs)
            if self.force_monotonic and not is_monotonic(pct_pos_bin):
                continue

            if self.force_mix_label and (max(pct_pos_bin) == 1 or min(pct_pos_bin) == 0):
                continue

            cur_metric = self.__class__.evaluate_cutoff(counter, candidate_cutoffs, cutoffs)
            if cur_metric > best_metric:
                best_metric, next_split = cur_metric, v

        if next_split is not None:
            bisect.insort(cutoffs, next_split)
            return cutoffs, True
        else:
            return cutoffs, False

    def _enforce_bin_constraints(self, counter, cutoffs):
        """ Enforce the monotonic and minimum sample percentage constraints """
        pct_pos_bin = counter.pct_pos_given_cutoffs(cutoffs, include_edge=False)
        n_sample_bin = counter.n_sample_given_cutoffs(cutoffs, include_edge=False)
        pct_sample_bin = n_sample_bin / counter.total_sample_with_null

        while len(cutoffs) > 2 and min(pct_sample_bin) < self.min_interval_size:
            min_idx = np.argmin(pct_sample_bin)

            # cutoffs =  [0, 1, 2, 3, 4]
            # bin_idx =    [0, 1, 2, 3]
            # pick one of cutoff points at (min_idx, min_idx+1) to drop
            if min_idx == 0:
                _ = cutoffs.pop(1)
            elif min_idx == len(pct_sample_bin):
                _ = cutoffs.pop(-1)
            else:
                # merge with the bin with smaller population
                if n_sample_bin[min_idx-1] < n_sample_bin[min_idx]:
                    _ = cutoffs.pop(min_idx)
                else:
                    _ = cutoffs.pop(min_idx+1)
     
            pct_pos_bin = counter.pct_pos_given_cutoffs(cutoffs, include_edge=False)
            n_sample_bin = counter.n_sample_given_cutoffs(cutoffs, include_edge=False)
            pct_sample_bin = n_sample_bin / counter.total_sample_with_null

        return cutoffs              

    def _fit(self, X, y, **fit_parmas):
        """ Fit a single feature and return the cutoff points"""
        self.categorical_cols = self.categorical_cols or []

        if not is_numeric_dtype(X) and X.name not in self.categorical_cols:
            raise ValueError('Column {} is not numeric and not in categorical_cols.'.format(X.name))

        y = force_zero_one(y)
        X, y = make_series(X), make_series(y)

        # if X is discrete, encode with positive ratio in y
        if X.name in self.categorical_cols:
            # the categorical columns will remain unchanged if
            # we turn off  bin_cat_cols
            if not self.bin_cat_cols:
                return None
            X = self.encode_with_label(X, y)

        n_bins = X.nunique()
        
        if n_bins < self.max_bin:
            counter = CumulativeCounter(X, y, bins=None)
            cutoffs = sorted(X.dropna().unique())
        else:
            # create the counter and initialize cutoff points
            # also create a flag indicating whether the binning process should stop
            counter = CumulativeCounter(X, y, bins=self.prebin if n_bins > self.prebin else None)
            cutoffs = list()
            continue_flag = True

            while continue_flag and len(cutoffs) + 1 < self.max_bin:
                cutoffs, continue_flag = self._find_single_split(counter, cutoffs)
            cutoffs = [X.min()] + cutoffs

        cutoffs = self._enforce_bin_constraints(counter, cutoffs)
        return cutoffs


def _ks_score(counter: CumulativeCounter, candidate_cutoffs: list, cutoffs: list=None):
    def _diff(counter, cutoff):
        n_sample, n_pos = counter[cutoff]
        n_neg = n_sample - n_pos
        total_neg = counter.total_sample - counter.total_pos

        pct_pos = n_pos / counter.total_pos
        pct_neg = n_neg / counter.total_neg
        return abs(pct_pos - pct_neg)
    return max([_diff(counter, i) for i in candidate_cutoffs])


class KSBinning(TopDownBinning):
    evaluate_cutoff = _ks_score



def _info_gain(counter: CumulativeCounter, candidate_cutoffs: list, cutoffs: list): 
    return counter.entropy_given_cutoffs(candidate_cutoffs) - counter.entropy_given_cutoffs(cutoffs)


class EntropyBinning(TopDownBinning):
    evaluate_cutoff = _info_gain



def _calc_iv(counter: CumulativeCounter, candidate_cutoffs: list, cutoffs: list=None):
    return counter.iv_given_cutoffs(candidate_cutoffs)


class IVBinning(TopDownBinning):
    evaluate_cutoff = _calc_iv



class ChiSquareBinning(SupervisedBinning):

    def __init__(self,
                 max_bin: int,
                 cols: list = None,
                 bins: dict = None,
                 categorical_cols: List[str] = None,
                 bin_cat_cols: bool = True,
                 encode: bool = True,
                 fill: int = -1,
                 force_monotonic: bool = True,
                 force_mix_label: bool = True,
                 min_interval_size: float = 0.02,
                 strict: bool = True,
                 ignore_na: bool = True,
                 prebin: int = 100,
                 prebin_method: str = 'tree',
                 min_frac=0.01):
        """
        :param max_bin: The number of bins to split into
        :param cols: A list of columns to perform binning, if set to None, perform binning on all columns.
        :param bins: A dictionary mapping column name to cutoff points
        :param categorical_cols: A list of categorical columns
        :param bin_cat_cols: Whether to perform binning on categorical columns
        :param encode: If set to False, the result of transform will be right cutoff point of the interval
        :param fill: Value used for inputing missing value
        :param force_monotonic:  Whether to force the bins to be monotonic with the
                positive proportion of the label
        :param force_mix_label:  Whether to force all the bins to have mix labels
        :param min_interval_size: The minimum percentage of samples within a single interval
        :param strict: If set to True, equal values will not be treated as monotonic
        :param ignore_na: The monotonicity check will ignore missing value
        :param prebin: An integer, number of bins to split into before the chimerge process.
        :param prebin_method: A string, indicating which binner is used for prebinning. 'tree' for
                TreeBinner and 'equal_freq` for using equal frequency binning.
        :param min_frac: Minimum fraction of samples within each bin. Only supported when the prebin_method
                is 'tree'. Either a float or a mapping from column name to its minimum fraction

        Usage:
        --------------
        >>> from sklearn.datasets import load_breast_cancer
        >>> from sklearn.linear_model import LogisticRegression
        >>> X, y = load_breast_cancer(return_X_y=True)
        >>> CB = ChiSquareBinning(max_bin=5, force_monotonic=True, force_mix_label=True)
        >>> encoded = CB.fit_transform(X, y)
        >>> CB.bins # get the cutoff points
        """
        super().__init__(cols, bins, categorical_cols, encode, fill)
        self.max_bin = max_bin
        self.bin_cat_cols = bin_cat_cols
        self.force_monotonic = force_monotonic
        self.force_mix_label = force_mix_label
        self.min_interval_size = min_interval_size
        self.strict = strict
        self.ignore_na = ignore_na

        self.prebin = prebin
        self.prebin_method = prebin_method
        self.min_frac = min_frac

        self._chisquare_cache = dict()

    def calculate_chisquare(self, mapping: Dict[int, list], candidates: Iterable) -> float:
        # try to get from the cache first
        unique_x = frozenset(candidates)
        if self._chisquare_cache.get(unique_x, False):
            return self._chisquare_cache[unique_x]

        count = {k: len(v) for k, v in mapping.items()}
        actual_pos = {k: sum(v) for k, v in mapping.items()}
        actual_neg = {k: (count[k] - actual_pos[k]) for k in candidates}

        expected_ratio = self.expected_ratio
        expected_pos = {k: v * expected_ratio for k, v in count.items()}
        expected_neg = {k: (count[k] - expected_pos[k]) for k in candidates}

        chi2 = sum((actual_pos[k] - expected_pos[k])**2 / expected_pos[k] + \
                   (actual_neg[k] - expected_neg[k])**2 / expected_neg[k]
                   for k in candidates)
        dgfd = len(candidates) - 1
        chi2 = chi2 / dgfd
        self._chisquare_cache[unique_x] = chi2
        return chi2

    @staticmethod
    def find_candidate(values: Iterable, target: int) -> list:
        """ Return a list of candidatate values that's next bigger or next smaller than the target value.
            The candidate list will have only one element when the target is the min or max in X.
            ex. find_candidate([1, 2, 3, 0], 2) => [1, 3]
        """
        values = sorted(values)
        idx = values.index(target)
        cnt = len(values)
        return [values[i] for i in [idx - 1, idx + 1] if 0 <= i < cnt]

    def encode_with_label(self, X: pd.Series, y: pd.Series) -> pd.Series:
        """ Encode categorical features with its percentage of positive samples"""
        X, y = make_series(X), make_series(y)
        pct_pos = y.groupby(X).mean()
        # save the mapping for transform()
        self.discrete_encoding[X.name] = pct_pos
        return X.map(pct_pos)

    def is_monotonic_post_bin(self, mapping: Dict[int, list]):
        """ Check whether the proportion of positive label is monotonic to bin value"""
        pct_pos = sorted([(k, np.mean(v)) for k, v in mapping.items()])
        return self.is_monotonic([i[1] for i in pct_pos], self.strict, self.ignore_na)

    def merge_bin(self, mapping: Dict[int, list], replace_value, original_value) -> Dict[int, list]:
        """ Replace the smaller value with the bigger one except when the replace value is the
            minimum of X, that case we replace the bigger value with the the smaller one.
        """
        def _replace(mapping: Dict[int, list], to_replace: int, value: int):
            mapping[value].extend(mapping[to_replace])
            del mapping[to_replace]
            return mapping

        if replace_value > original_value:
            original_value, replace_value = replace_value, original_value

        return _replace(mapping, original_value, replace_value)

        # make sure replace_value is the bigger one
        # if replace_value < original_value:
        #     replace_value, original_value = original_value, replace_value

        # if original_value == min(mapping):
        #     return _replace(mapping, replace_value, original_value)
        # else:
        #     return _replace(mapping, original_value, replace_value)

    def merge_chisquare(self, mapping: Dict[int, list], candidates=None) -> Dict[int, list]:
        """ Performs a single merge based on chi square value
            returns a new X' with new groups
        :param candidates: the candidate values that are allowed to merge, default set to all the values
        """
        candidates = candidates or mapping.keys()
        candidate_pairs = self.sorted_two_gram(candidates)

        # find the pair with minimum chisquare in one-pass
        min_idx, min_chi2 = 0, np.inf
        for i, pair in enumerate(candidate_pairs):
            chi2 = self.calculate_chisquare(mapping, pair)
            if chi2 < min_chi2:
                min_idx, min_chi2 = i, chi2

        # replace the smaller value with the bigger one except for the minimum pair
        small, large = candidate_pairs[min_idx]
        return self.merge_bin(mapping, small, large)

    def merge_monotonic(self, mapping: Dict[int, list]) -> Dict[int, list]:
        """ Performs a single chimerge and check the monotonicity afterwards
            Prefer to merge the bins with smaller sample size 
        """
        # pick the bin with smallest sample size
        k = sorted([(len(v), k) for k, v in mapping.items()])[0][1]
        merge_candidates = self.find_candidate(mapping.keys(), k)
        mapping = self.merge_chisquare(mapping, merge_candidates + [k])
        return mapping
        
    def merge_interval_size(self, mapping: Dict[int, list], min_samples: int) -> Dict[int, list]:
        """ Performs a single merge trying to merge bins that have fewer samples 
            than self.min_inteval_size
        """
        # convert to list so we don't get error modifying dictionary during loop
        for k in sorted(list(mapping.keys())):
            n_sample = len(mapping[k])

            if n_sample < min_samples:
                merge_candidates = self.find_candidate(mapping.keys(), k)

                # merge to the neighbour interval with fewer samples
                if len(merge_candidates) == 1:
                    return self.merge_bin(mapping, merge_candidates[0], k), False
                else:
                    left_cand, right_cand = merge_candidates
                    n_left, n_right = len(mapping[left_cand]), len(mapping[right_cand])

                    if n_left < n_right:
                        return self.merge_bin(mapping, left_cand, k), False
                    elif n_left > n_right:
                        return self.merge_bin(mapping, right_cand, k), False
                    else:
                        return self.merge_chisquare(mapping, [left_cand, k, right_cand]), False
        else:
            return mapping, True

    def merge_purity(self, mapping: Dict[int, list]) -> Tuple[Dict[int, list], bool]:
        """ Performs a single merge trying to merge bins with only 0 or a label into the adjacent mixed label bin
            Return the updated mapping and a purity label
        """
        # convert to list so we don't get error modifying dictionary during loop
        for k in sorted(list(mapping.keys())):
            pct_pos = np.mean(mapping[k])

            if pct_pos * (1 - pct_pos) == 0:
                merge_candidates = self.find_candidate(mapping.keys(), k)

                # it merges to the candidate that has mix labels, if both candidates do, then
                # it will merge to the larger value, if neither does, if will merge both candidates
                if len(merge_candidates) == 1:
                    return self.merge_bin(mapping, merge_candidates[0], k), False
                else:
                    left_cand, right_cand = merge_candidates
                    pct_pos_left = np.mean(mapping[left_cand])
                    pct_pos_right = np.mean(mapping[right_cand])

                    can_merge_left = 0 < (pct_pos_left + pct_pos) / 2 < 1
                    can_merge_right = 0 < (pct_pos_right + pct_pos) / 2 < 1

                    if can_merge_left and can_merge_right:
                        return self.merge_chisquare(mapping, [left_cand, k, right_cand]), False
                    elif can_merge_left:
                        return self.merge_bin(mapping, left_cand, k), False
                    elif can_merge_right:
                        return self.merge_bin(mapping, right_cand, k), False
                    else:
                        mapping = self.merge_bin(mapping, left_cand, k)
                        return self.merge_bin(mapping, right_cand, k), False
        else:
            return mapping, True

    def _fit(self, X, y, **fit_parmas):
        """ Fit a single feature and return the cutoff points"""
        self.categorical_cols = self.categorical_cols or []

        if not is_numeric_dtype(X) and X.name not in self.categorical_cols:
            raise ValueError('Column {} is not numeric and not in categorical_cols.'.format(X.name))

        y = force_zero_one(y)
        X, y = make_series(X), make_series(y)

        # if X is discrete, encode with positive ratio in y
        if X.name in self.categorical_cols:
            # the categorical columns will remain unchanged if
            # we turn off  bin_cat_cols
            if not self.bin_cat_cols:
                return None
            X = self.encode_with_label(X, y)

        # the number of bins is the number of cutoff points minus 1
        n_bins = X.nunique() - 1

        # if the number of bins is less than `max_bin` for categorical columns then
        # set the column as a mapping
        if n_bins < self.max_bin and X.name in self.categorical_cols:
            # mapping bad rate to encoding
            group_mapping = {v: i+1 for i, v in enumerate(set(X[X.notnull()]))}
            return self.discrete_encoding[X.name].map(group_mapping).to_dict()

        # speed up the process with prebinning
        if self.prebin and n_bins > self.prebin:
            if self.prebin_method.lower() == 'tree':
                min_frac = self.min_frac if is_number(self.min_frac) else self.min_frac[X.name]
                X, _ = tree_binning(X, y, n=self.prebin, min_frac=min_frac, 
                                    encode=False, random_state=1024)
            elif self.prebin_method.lower() == 'equal_freq':
                X, _ = equal_frequency_binning(X, n=self.prebin, encode=False)
            else:
                raise ValueError('Only `tree` and `equal_freq` is supported for prebin_method.')

        # convert to mapping
        mapping = y.groupby(X).apply(list).to_dict()

        # set the overall expected ratio
        if len(mapping) == 0:
            return [-np.inf]

        self.expected_ratio = sum(sum(v) for v in mapping.values()) / sum(len(v) for v in mapping.values())
        # if the expected_ratio is 0 or 1 there should be only 1 group and
        # any not-null value will be encoded into 1
        if self.expected_ratio == 0 or self.expected_ratio == 1:
            return [-np.inf]

        n_bins = len(mapping)
        # merge bins based on chi square
        while n_bins > self.max_bin:
            mapping = self.merge_chisquare(mapping)
            n_bins = len(mapping)

        # merge bins to create mixed label in every bin
        if self.force_mix_label and n_bins > 1:
            is_pure = False
            while not is_pure:
                mapping, is_pure = self.merge_purity(mapping)

        # merge bins to keep bins to be monotonic
        if self.force_monotonic:
            while len(mapping) > 2 and not self.is_monotonic_post_bin(mapping):
                # mapping = self.merge_chisquare(mapping)
                mapping = self.merge_monotonic(mapping)

        # merge bins to meet the minimum sample size for each interval
        if self.min_interval_size > 0:
            if self.min_interval_size <= 1:
                min_interval_size = self.min_interval_size * X.notnull().sum()
            else:
                min_interval_size = self.min_interval_size

            meet_interval_size = False
            while not meet_interval_size and len(mapping) > 2:
                mapping, meet_interval_size = self.merge_interval_size(mapping, min_interval_size)

        # clean up the cache
        self._chisquare_cache = dict()
        return mapping.keys()


if __name__ == '__main__':
    import random
    import pickle
    import time

    X = [0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 6, 6, 6, 7, 7, 7, 7, 7]
    y = [0, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 1, 1, 1, 1, 0]
    X = pd.DataFrame({'a': X, 'b': X, 'c': X})

    X = pd.DataFrame({'a': [random.randint(1, 20) for _ in range(1000)],
                   'b': [random.randint(1, 20) for _ in range(1000)],
                 'c': [random.randint(1, 20) for _ in range(1000)]})
    y = [int(random.random() > 0.5) for _ in range(1000)]

    CB = ChiSquareBinning(max_bin=5, categorical_cols=['a'], force_mix_label=False, force_monotonic=False,
                          prebin=100, encode=True, strict=False)

    start = time.time()
    CB.fit(X, y)
    print(time.time() - start)
    print(CB.transform(X).nunique())
    print(CB.bins)
    print(pd.concat([X, CB.transform(X)], axis=1))
