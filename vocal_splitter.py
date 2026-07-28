#!/usr/bin/env python3
import os, sys

# Must be set before torch is imported so Demucs caches model weights into models/
os.environ.setdefault('TORCH_HOME', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models'))

import time, shutil
import argparse, requests, subprocess

import numpy as np
import soundfile as sf
import torch
from demucs.pretrained import get_model
from demucs.apply import apply_model
import librosa

from constants import media_types


def ffm_wav2m4a(input_fn, output_fn, br='128k'):
	input_fn, output_fn = [fn.replace('"', '\\"') for fn in [input_fn, output_fn]]
	return subprocess.run(['ffmpeg', '-y', '-i', input_fn, '-c:a', 'aac', '-b:a', br, output_fn]).returncode == 0


def ffm_video2wav(input_fn, output_fn):
	# Resample to the model's native rate (44100) at extraction time
	input_fn, output_fn = [fn.replace('"', '\\"') for fn in [input_fn, output_fn]]
	return subprocess.run(['ffmpeg', '-y', '-i', input_fn, '-f', 'wav', '-ar', '44100', output_fn]).returncode == 0


def is_supported_media_file(filename):
	return os.path.splitext(filename)[1].lower() in media_types


def split_vocal_by_stereo(in_wav, out_wav_nonvocal, out_wav_vocal):
	try:
		X, sr = librosa.load(in_wav, sr=44100, mono=False, dtype=np.float32, res_type='kaiser_fast')
		if X.shape[0] < 2:
			return False
		if out_wav_nonvocal:
			sf.write(out_wav_nonvocal, X[0, :] - X[1, :], sr)
		if out_wav_vocal:
			sf.write(out_wav_vocal, X[0, :] + X[1, :], sr)
		return True
	except:
		return False


def split_vocal_by_dnn(in_wav, out_wav_nonvocal, out_wav_vocal, args):
	sr = args.model.samplerate

	print('Loading wave source ...', end=' ', flush=True)
	wav, _ = librosa.load(in_wav, sr=sr, mono=False, dtype=np.float32, res_type='kaiser_fast')
	print('done', flush=True)

	if wav.ndim == 1:
		wav = np.stack([wav, wav])

	# Demucs expects [batch, channels, samples]
	wav_tensor = torch.from_numpy(wav).unsqueeze(0).to(args.device)

	print('Separating with Demucs ...', flush=True)
	# --tta maps to random waveform shifts (test-time augmentation in Demucs)
	shifts = 2 if args.tta else 0
	with torch.no_grad():
		sources = apply_model(
			args.model, wav_tensor,
			shifts=shifts,
			split=True,       # process in overlapping chunks (equivalent to the old cropsize loop)
			overlap=0.25,
			progress=True,
			device=args.device,
			num_workers=0,
		)

	# sources: [batch=1, n_stems, channels, samples]  →  [n_stems, channels, samples]
	sources = sources.squeeze(0).cpu().numpy()

	vocals_idx      = args.model.sources.index('vocals')
	non_vocals_idx  = [i for i in range(len(args.model.sources)) if i != vocals_idx]

	# Sum all non-vocal stems (drums, bass, other, …) into the instrument track
	instruments = sources[non_vocals_idx].sum(axis=0).T   # [samples, channels]
	vocals      = sources[vocals_idx].T                   # [samples, channels]

	print('Writing instrument track ...', end=' ', flush=True)
	sf.write(out_wav_nonvocal, instruments, sr)
	print('done', flush=True)

	if out_wav_vocal:
		print('Writing vocal track ...', end=' ', flush=True)
		sf.write(out_wav_vocal, vocals, sr)
		print('done', flush=True)


song_path = ''
last_completed = ''
use_DNN = True


def get_next_file(cuda_device):
	global song_path, use_DNN, last_completed
	try:
		obj = requests.get(f'http://localhost:5000/get_vocal_todo_list/{cuda_device.type}/{last_completed}').json()
		song_path = obj['download_path'].rstrip('/')
		use_DNN = obj['use_DNN']
	except:
		if not song_path:
			print('PiKaraoke is not running and --download-path is not specified, exiting ...')
			sys.exit()
		obj = {'queue': []}

	if not os.path.isdir(song_path+'/nonvocal') and not os.path.isdir(song_path+'/vocal'):
		return None
	for fn in obj['queue']:
		if not is_supported_media_file(fn):
			continue
		bn = ('' if use_DNN else '.')+os.path.basename(fn)
		if os.path.isdir(song_path+'/nonvocal') and not os.path.isfile(f'{song_path}/nonvocal/{bn}.m4a'):
			return os.path.basename(fn)
		if os.path.isdir(song_path+'/vocal') and not os.path.isfile(f'{song_path}/vocal/{bn}.m4a'):
			return os.path.basename(fn)

	# get from listing directory
	for bn in [i for i in os.listdir(song_path) if not i.startswith('.') and os.path.isfile(song_path+'/'+i)]:
		if not is_supported_media_file(bn):
			continue
		bn1 = ('' if use_DNN else '.')+bn
		if os.path.isdir(song_path+'/nonvocal') and not os.path.isfile(f'{song_path}/nonvocal/{bn1}.m4a'):
			return bn
		if os.path.isdir(song_path+'/vocal') and not os.path.isfile(f'{song_path}/vocal/{bn1}.m4a'):
			return bn

	return None


def main(argv):
	global song_path, last_completed

	p = argparse.ArgumentParser()
	p.add_argument('--download-path', '-d',
	               help='Path for downloaded songs. Will be overridden by the one from HTTP request. '
	                    'Set this to forcefully run the vocal-splitter even when PiKaraoke is not running.',
	               default='')
	p.add_argument('--gpu', '-g', type=int,
	               help='CUDA device ID for GPU inference, -1 to force CPU (default: use GPU if available)',
	               default=None)
	p.add_argument('--pretrained_model', '-P', type=str, default='htdemucs',
	               help='Demucs model name: htdemucs (default), htdemucs_ft, mdx_extra, mdx_extra_q, …')
	p.add_argument('--batchsize', '-B', type=int, default=4,
	               help='(Unused by Demucs — kept for CLI compatibility)')
	p.add_argument('--postprocess', '-p', action='store_true',
	               help='(Unused by Demucs — kept for CLI compatibility)')
	p.add_argument('--tta', '-t', action='store_true',
	               help='Test-time augmentation: average over 10 random time shifts')
	p.add_argument('--ramdir', '-rd',
	               help='Temporary directory on RAMDISK to reduce I/O load',
	               default='z:/' if sys.platform.startswith('win') else '/dev/shm')
	args = p.parse_args(argv)

	song_path = os.path.expanduser(args.download_path).rstrip('/')

	# Load Demucs model — weights are cached under models/ via TORCH_HOME set at top of file
	print(f'Loading Demucs model "{args.pretrained_model}" ...', end=' ', flush=True)
	model = get_model(args.pretrained_model)
	model.eval()

	device = torch.device('cpu')
	if (args.gpu is None or args.gpu >= 0) and torch.cuda.is_available():
		device = torch.device(f'cuda:{0 if args.gpu is None else args.gpu}')
	model.to(device)

	args.model  = model
	args.device = device
	print('done', flush=True)
	print(f'  stems : {model.sources}', flush=True)
	print(f'  device: {device}', flush=True)

	# set song_path global variable from local server
	get_next_file(device)

	# Use RAMDISK for large temporary .wav files if available
	RAMDIR = args.ramdir if os.path.isdir(args.ramdir) else song_path

	in_wav, out_wav_vocal, out_wav_nonvocal, out_m4a_vocal, out_m4a_nonvocal = \
		[f'{RAMDIR}/.input.wav', f'{RAMDIR}/.vocal.wav', f'{RAMDIR}/.nonvocal.wav',
		 f'{RAMDIR}/.vocal.m4a', f'{RAMDIR}/.nonvocal.m4a']

	# Main loop
	while True:
		next_file = get_next_file(device)
		if not next_file:
			time.sleep(2)
			continue

		print(f'Start processing {next_file} :')
		if not ffm_video2wav(song_path+'/'+next_file, in_wav):
			print(f'Skipping {next_file}: could not extract an audio stream', flush=True)
			last_completed = next_file
			continue

		if use_DNN:
			split_vocal_by_dnn(in_wav, out_wav_nonvocal, out_wav_vocal, args)
			if os.path.isdir(song_path+'/nonvocal'):
				ffm_wav2m4a(out_wav_nonvocal, out_m4a_nonvocal)
				shutil.move(out_m4a_nonvocal, f'{song_path}/nonvocal/{next_file}.m4a')
			if os.path.isdir(song_path+'/vocal'):
				ffm_wav2m4a(out_wav_vocal, out_m4a_vocal)
				shutil.move(out_m4a_vocal, f'{song_path}/vocal/{next_file}.m4a')
		else:
			split_vocal_by_stereo(in_wav, out_wav_nonvocal, out_wav_vocal)
			if os.path.isdir(song_path+'/nonvocal'):
				ffm_wav2m4a(out_wav_nonvocal, out_m4a_nonvocal)
				shutil.move(out_m4a_nonvocal, f'{song_path}/nonvocal/.{next_file}.m4a')
			if os.path.isdir(song_path+'/vocal'):
				ffm_wav2m4a(out_wav_vocal, out_m4a_vocal)
				shutil.move(out_m4a_vocal, f'{song_path}/vocal/.{next_file}.m4a')
		last_completed = next_file


if __name__ == '__main__':
	main(sys.argv[1:])
