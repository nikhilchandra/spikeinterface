import shutil
import json
import pickle
import warnings
from pathlib import Path

import numpy as np

from sklearn.decomposition import IncrementalPCA

from spikeinterface.core.core_tools import check_json
from spikeinterface.core.job_tools import ChunkRecordingExecutor, ensure_n_jobs
from spikeinterface.core import WaveformExtractor
from .template_tools import get_template_channel_sparsity

_possible_modes = ['by_channel_local', 'by_channel_global', 'concatenated']


class WaveformPrincipalComponent:
    """
    Class to extract principal components from a WaveformExtractor object.

    Parameters
    ----------
    waveform_extractor: WaveformExtractor
        The WaveformExtractor object

    Returns
    -------
    pc: WaveformPrincipalComponent
        The WaveformPrincipalComponent object

    Examples
    --------
    >>> we = si.extract_waveforms(recording, sorting, folder='waveforms_mearec')
    >>> pc = st.compute_principal_components(we, load_if_exists=True, n_components=3, mode='by_channel_local')
    >>> projections = pc.get_projections(unit_id=1)
    >>> all_projections = pc.get_all_projections()

    """

    def __init__(self, waveform_extractor):
        self.waveform_extractor = waveform_extractor

        self.folder = self.waveform_extractor.folder

        self._params = {}
        if (self.folder / 'params_pca.json').is_file():
            with open(str(self.folder / 'params_pca.json'), 'r') as f:
                self._params = json.load(f)

    @classmethod
    def load_from_folder(cls, folder):
        we = WaveformExtractor.load_from_folder(folder)
        pc = WaveformPrincipalComponent(we)
        return pc

    @classmethod
    def create(cls, waveform_extractor):
        pc = WaveformPrincipalComponent(waveform_extractor)
        return pc

    def __repr__(self):
        we = self.waveform_extractor
        clsname = self.__class__.__name__
        nseg = we.recording.get_num_segments()
        nchan = we.recording.get_num_channels()
        txt = f'{clsname}: {nchan} channels - {nseg} segments'
        if len(self._params) > 0:
            mode = self._params['mode']
            n_components = self._params['n_components']
            txt = txt + f'\n  mode:{mode} n_components:{n_components}'
        return txt

    def _reset(self):
        self._components = {}
        self._params = {}

        pca_folder = self.folder / 'PCA'
        if pca_folder.is_dir():
            shutil.rmtree(pca_folder)
        pca_folder.mkdir()

    def set_params(self, n_components=5, mode='by_channel_local',
                   whiten=True, dtype='float32'):
        """
        Set parameters for waveform extraction

        Parameters
        ----------
        n_components:  int
        
        mode : 'by_channel_local' / 'by_channel_global' / 'concatenated'
        
        whiten: bool
            params transmitted to sklearn.PCA
        
        """
        self._reset()

        assert mode in _possible_modes

        self._params = dict(
            n_components=int(n_components),
            mode=str(mode),
            whiten=bool(whiten),
            dtype=np.dtype(dtype).str)

        (self.folder / 'params_pca.json').write_text(
            json.dumps(check_json(self._params), indent=4), encoding='utf8')

    def get_projections(self, unit_id):
        proj_file = self.folder / 'PCA' / f'pca_{unit_id}.npy'
        proj = np.load(proj_file)
        return proj

    def get_components(self, unit_id):
        warnings.warn("The 'get_components()' function has been substituted by the 'get_projections()' "
                      "function and it will be removed in the next release", warnings.DeprecationWarning)
        return self.get_projections(unit_id)

    def get_pca_model(self):
        """
        Returns the scikit-learn PCA model objects.

        Returns
        -------
        all_pca: PCA object(s)
            * if mode is "by_channel_local", "all_pca" is a list of PCA model by channel
            * if mode is "by_channel_global" or "concatenated", "all_pca" is a single PCA model

        """
        mode = self._params["mode"]
        all_pca = None
        if mode == "by_channel_local":
            all_pca = []
            for chan_id in self.waveform_extractor.recording.channel_ids:
                pca_file = self.folder / "PCA" / f"pca_model_{mode}_{chan_id}.pkl"
                pca = pickle.load(pca_file.open("rb"))
                all_pca.append(pca)
        elif mode == "by_channel_global":
            pca_file = self.folder / "PCA" / f"pca_model_{mode}.pkl"
            all_pca = pickle.load(pca_file.open("rb"))
        elif mode == "concatenated":
            pca_file = self.folder / "PCA" / f"pca_model_{mode}.pkl"
            all_pca = pickle.load(pca_file.open("rb"))
        return all_pca

    def get_all_projections(self, channel_ids=None, unit_ids=None, outputs='id'):
        recording = self.waveform_extractor.recording

        if unit_ids is None:
            unit_ids = self.waveform_extractor.sorting.unit_ids

        all_labels = []  #  can be unit_id or unit_index
        all_projections = []
        for unit_index, unit_id in enumerate(unit_ids):
            proj = self.get_projections(unit_id)
            if channel_ids is not None:
                chan_inds = recording.ids_to_indices(channel_ids)
                proj = proj[:, :, chan_inds]
            n = proj.shape[0]
            if outputs == 'id':
                labels = np.array([unit_id] * n)
            elif outputs == 'index':
                labels = np.ones(n, dtype='int64')
                labels[:] = unit_index
            all_labels.append(labels)
            all_projections.append(proj)
        all_labels = np.concatenate(all_labels, axis=0)
        all_projections = np.concatenate(all_projections, axis=0)

        return all_labels, all_projections

    def get_all_components(self, channel_ids=None, unit_ids=None, outputs='id'):
        warnings.warn("The 'get_all_components()' function has been substituted by the 'get_all_projections()' "
                      "function and it will be removed in the next release", warnings.DeprecationWarning)
        return self.get_all_projections(channel_ids=channel_ids, unit_ids=unit_ids, outputs=outputs)

    def project_new(self, new_waveforms):
        """
        Projects new waveforms or traces snippets on the PC components.

        Parameters
        ----------
        new_waveforms: np.array
            Array with new waveforms to project with shape (num_waveforms, num_samples, num_channels)

        Returns
        -------
        projections: np.array


        """
        p = self._params
        mode = p["mode"]

        # check waveform shapes
        wfs0 = self.waveform_extractor.get_waveforms(unit_id=self.waveform_extractor.sorting.unit_ids[0])
        assert wfs0.shape[1] == new_waveforms.shape[1], "Mismatch in number of samples between waveforms used to fit" \
                                                        "the pca model and 'new_waveforms"
        assert wfs0.shape[2] == new_waveforms.shape[2], "Mismatch in number of channels between waveforms used to fit" \
                                                        "the pca model and 'new_waveforms"

        # get channel ids and pca models
        channel_ids = self.waveform_extractor.recording.channel_ids
        all_pca = self.get_pca_model()

        projections = None
        if mode == "by_channel_local":
            shape = (new_waveforms.shape[0], p['n_components'], len(channel_ids))
            projections = np.zeros(shape)
            for chan_ind, chan_id in enumerate(channel_ids):
                pca = all_pca[chan_ind]
                projections[:, :, chan_ind] = pca.transform(new_waveforms[:, :, chan_ind])
        elif mode == "by_channel_global":
            shape = (new_waveforms.shape[0], p['n_components'], len(channel_ids))
            projections = np.zeros(shape)
            for chan_ind, chan_id in enumerate(channel_ids):
                projections[:, :, chan_ind] = all_pca.transform(new_waveforms[:, :, chan_ind])
        elif mode == "concatenated":
            wfs_flat = new_waveforms.reshape(new_waveforms.shape[0], -1)
            projections = all_pca.transform(wfs_flat)

        return projections

    def run(self):
        """
        This compute the PCs on waveforms extacted within
        the WaveformExtarctor.
        It is only for some sampled spikes defined in WaveformExtarctor
        
        The index of spikes come from the WaveformExtarctor.
        This will be cached in the same folder than WaveformExtarctor
        in 'PCA' subfolder.
        """
        p = self._params
        we = self.waveform_extractor
        num_chans = we.recording.get_num_channels()

        # prepare memmap files with npy
        projection_memmap = {}
        unit_ids = we.sorting.unit_ids
        for unit_id in unit_ids:
            n_spike = we.get_waveforms(unit_id).shape[0]
            projection_file = self.folder / 'PCA' / f'pca_{unit_id}.npy'
            if p['mode'] in ('by_channel_local', 'by_channel_global'):
                shape = (n_spike, p['n_components'], num_chans)
            elif p['mode'] == 'concatenated':
                shape = (n_spike, p['n_components'])
            proj = np.zeros(shape, dtype=p['dtype'])
            np.save(projection_file, proj)
            comp = np.load(projection_file, mmap_mode='r+')
            projection_memmap[unit_id] = comp

        # run ...
        if p['mode'] == 'by_channel_local':
            self._run_by_channel_local(projection_memmap)
        elif p['mode'] == 'by_channel_global':
            self._run_by_channel_global(projection_memmap)
        elif p['mode'] == 'concatenated':
            self._run_concatenated(projection_memmap)

    def run_for_all_spikes(self, file_path, max_channels_per_template=16, peak_sign='neg',
                           **job_kwargs):
        """
        This run the PCs on all spikes from the sorting.
        This is a long computation because waveform need to be extracted from each spikes.
        
        Used mainly for `export_to_phy()`
        
        PCs are exported to a .npy single file.
        
        """
        p = self._params
        we = self.waveform_extractor
        sorting = we.sorting
        recording = we.recording

        assert sorting.get_num_segments() == 1
        assert p['mode'] in ('by_channel_local', 'by_channel_global')

        file_path = Path(file_path)

        all_spikes = sorting.get_all_spike_trains(outputs='unit_index')
        spike_times, spike_labels = all_spikes[0]

        max_channels_per_template = min(max_channels_per_template, we.recording.get_num_channels())

        best_channels_index = get_template_channel_sparsity(we, method='best_channels',
                                                            peak_sign=peak_sign, num_channels=max_channels_per_template,
                                                            outputs='index')

        unit_channels = [best_channels_index[unit_id] for unit_id in sorting.unit_ids]

        if p['mode'] == 'by_channel_local':
            all_pca = self._fit_by_channel_local()
        elif p['mode'] == 'by_channel_global':
            one_pca = self._fit_by_channel_global()
            all_pca = [one_pca] * recording.get_num_channels()

        # nSpikes, nFeaturesPerChannel, nPCFeatures
        # this come from  phy template-gui
        # https://github.com/kwikteam/phy-contrib/blob/master/docs/template-gui.md#datasets
        shape = (spike_times.size, p['n_components'], max_channels_per_template)
        all_pcs = np.lib.format.open_memmap(filename=file_path, mode='w+', dtype='float32', shape=shape)
        all_pcs_args = dict(filename=file_path, mode='r+', dtype='float32', shape=shape)

        # and run
        func = _all_pc_extractor_chunk
        init_func = _init_work_all_pc_extractor
        n_jobs = ensure_n_jobs(recording, job_kwargs.get('n_jobs', None))
        if n_jobs == 1:
            init_args = (recording,)
        else:
            init_args = (recording.to_dict(),)
        init_args = init_args + (all_pcs_args, spike_times, spike_labels, we.nbefore, we.nafter, unit_channels, all_pca)
        processor = ChunkRecordingExecutor(recording, func, init_func, init_args, job_name='extract PCs', **job_kwargs)
        processor.run()

    def _fit_by_channel_local(self):
        we = self.waveform_extractor
        p = self._params

        unit_ids = we.sorting.unit_ids
        channel_ids = we.recording.channel_ids

        # there is one PCA per channel for independent fit per channel
        all_pca = [IncrementalPCA(n_components=p['n_components'], whiten=p['whiten']) for _ in channel_ids]

        # fit
        for unit_id in unit_ids:
            wfs = we.get_waveforms(unit_id)
            if wfs.size == 0:
                continue
            for chan_ind, chan_id in enumerate(channel_ids):
                pca = all_pca[chan_ind]
                pca.partial_fit(wfs[:, :, chan_ind])

        # save
        mode = p["mode"]
        for chan_ind, chan_id in enumerate(channel_ids):
            pca = all_pca[chan_ind]
            with (self.folder / "PCA" / f"pca_model_{mode}_{chan_id}.pkl").open("wb") as f:
                pickle.dump(pca, f)

        return all_pca

    def _run_by_channel_local(self, projection_memmap):
        """
        In this mode each PCA is "fit" and "transform" by channel.
        The output is then (n_spike, n_components, n_channels)
        """
        we = self.waveform_extractor
        p = self._params

        unit_ids = we.sorting.unit_ids
        channel_ids = we.recording.channel_ids

        all_pca = self._fit_by_channel_local()

        # transform
        for unit_id in unit_ids:
            wfs = we.get_waveforms(unit_id)
            if wfs.size == 0:
                continue
            for chan_ind, chan_id in enumerate(channel_ids):
                pca = all_pca[chan_ind]
                proj = pca.transform(wfs[:, :, chan_ind])
                projection_memmap[unit_id][:, :, chan_ind] = proj

    def _fit_by_channel_global(self):
        we = self.waveform_extractor
        p = self._params

        unit_ids = we.sorting.unit_ids
        channel_ids = we.recording.channel_ids

        # there is one unique PCA accross channels
        one_pca = IncrementalPCA(n_components=p['n_components'], whiten=p['whiten'])

        # fit
        for unit_id in unit_ids:
            wfs = we.get_waveforms(unit_id)
            if wfs.size == 0:
                continue
            for chan_ind, chan_id in enumerate(channel_ids):
                one_pca.partial_fit(wfs[:, :, chan_ind])

        # save
        mode = p["mode"]
        with (self.folder / "PCA" / f"pca_model_{mode}.pkl").open("wb") as f:
            pickle.dump(one_pca, f)

        return one_pca

    def _run_by_channel_global(self, projection_memmap):
        """
        In this mode there is one "fit" for all channels.
        The transform is applied by channel.
        The output is then (n_spike, n_components, n_channels)
        """
        we = self.waveform_extractor
        p = self._params

        unit_ids = we.sorting.unit_ids
        channel_ids = we.recording.channel_ids

        one_pca = self._fit_by_channel_global()

        # transform
        for unit_id in unit_ids:
            wfs = we.get_waveforms(unit_id)
            if wfs.size == 0:
                continue
            for chan_ind, chan_id in enumerate(channel_ids):
                proj = one_pca.transform(wfs[:, :, chan_ind])
                projection_memmap[unit_id][:, :, chan_ind] = proj

    def _run_concatenated(self, projection_memmap):
        """
        In this mode the waveforms are concatenated and there is
        a global fit_transform at once.
        """
        we = self.waveform_extractor
        p = self._params

        unit_ids = we.sorting.unit_ids

        # there is one unique PCA accross channels
        pca = IncrementalPCA(n_components=p['n_components'], whiten=p['whiten'])

        # fit
        for unit_id in unit_ids:
            wfs = we.get_waveforms(unit_id)
            wfs_flat = wfs.reshape(wfs.shape[0], -1)
            pca.partial_fit(wfs_flat)

        # save
        mode = p["mode"]
        with (self.folder / "PCA" / f"pca_model_{mode}.pkl").open("wb") as f:
            pickle.dump(pca, f)

        # transform
        for unit_id in unit_ids:
            wfs = we.get_waveforms(unit_id)
            wfs_flat = wfs.reshape(wfs.shape[0], -1)
            proj = pca.transform(wfs_flat)
            projection_memmap[unit_id][:, :] = proj


def _all_pc_extractor_chunk(segment_index, start_frame, end_frame, worker_ctx):
    recording = worker_ctx['recording']
    all_pcs = worker_ctx['all_pcs']
    spike_times = worker_ctx['spike_times']
    spike_labels = worker_ctx['spike_labels']
    nbefore = worker_ctx['nbefore']
    nafter = worker_ctx['nafter']
    unit_channels = worker_ctx['unit_channels']
    all_pca = worker_ctx['all_pca']

    seg_size = recording.get_num_samples(segment_index=segment_index)

    i0 = np.searchsorted(spike_times, start_frame)
    i1 = np.searchsorted(spike_times, end_frame)

    if i0 != i1:
        # protect from spikes on border :  spike_time<0 or spike_time>seg_size
        # usefull only when max_spikes_per_unit is not None
        # waveform will not be extracted and a zeros will be left in the memmap file
        while (spike_times[i0] - nbefore) < 0 and (i0 != i1):
            i0 = i0 + 1
        while (spike_times[i1 - 1] + nafter) > seg_size and (i0 != i1):
            i1 = i1 - 1

    if i0 == i1:
        return

    start = int(spike_times[i0] - nbefore)
    end = int(spike_times[i1 - 1] + nafter)
    traces = recording.get_traces(start_frame=start, end_frame=end, segment_index=segment_index)

    for i in range(i0, i1):
        st = spike_times[i]
        if st - start - nbefore < 0:
            continue
        if st - start + nafter > traces.shape[0]:
            continue

        wf = traces[st - start - nbefore:st - start + nafter, :]

        unit_index = spike_labels[i]
        chan_inds = unit_channels[unit_index]

        for c, chan_ind in enumerate(chan_inds):
            w = wf[:, chan_ind]
            if w.size > 0:
                w = w[None, :]
                all_pcs[i, :, c] = all_pca[chan_ind].transform(w)


def _init_work_all_pc_extractor(recording, all_pcs_args, spike_times, spike_labels, nbefore, nafter, unit_channels,
                                all_pca):
    worker_ctx = {}
    if isinstance(recording, dict):
        from spikeinterface.core import load_extractor
        recording = load_extractor(recording)
    worker_ctx['recording'] = recording
    worker_ctx['all_pcs'] = np.lib.format.open_memmap(**all_pcs_args)
    worker_ctx['spike_times'] = spike_times
    worker_ctx['spike_labels'] = spike_labels
    worker_ctx['nbefore'] = nbefore
    worker_ctx['nafter'] = nafter
    worker_ctx['unit_channels'] = unit_channels
    worker_ctx['all_pca'] = all_pca

    return worker_ctx


def compute_principal_components(waveform_extractor, load_if_exists=False,
                                 n_components=5, mode='by_channel_local',
                                 whiten=True, dtype='float32'):
    """
    Compute PC scores from waveform extractor.

    Parameters
    ----------
    waveform_extractor: WaveformExtractor
        The waveform extractor
    load_if_exists: bool
        If True and pc scores are already in the waveform extractor folders, pc scores are loaded and not recomputed.
    n_components: int
        Number of components fo PCA
    mode: str
        - 'by_channel_local': a local PCA is fitted for each channel (projection by channel)
        - 'by_channel_global': a global PCA is fitted for all channels (projection by channel)
        - 'concatenated': channels are concatenated and a global PCA is fitted
    whiten: bool
        If True, waveforms are pre-whitened
    dtype: dtype
        Dtype of the pc scores (default float32)

    Returns
    -------
    pc: WaveformPrincipalComponent
        The waveform principal component object
    """

    folder = waveform_extractor.folder
    if load_if_exists and folder.is_dir() and (folder / 'PCA').is_dir():
        pc = WaveformPrincipalComponent.load_from_folder(folder)
    else:
        pc = WaveformPrincipalComponent.create(waveform_extractor)
        pc.set_params(n_components=n_components, mode=mode, whiten=whiten, dtype=dtype)
        pc.run()

    return pc
