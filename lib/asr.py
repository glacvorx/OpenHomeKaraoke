#!/usr/bin/env python3

import os, sys


class ASR:
	def __init__(self, compo_name='whisper:base', verbose=True) -> None:
		bk_name, model_name, bk_bit = (compo_name.split(':')+['',''])[:3]
		if bk_name == 'faster_whisper':
			from faster_whisper import WhisperModel
			model_name = model_name or 'large-v3'
			compute_type = bk_bit or 'int8'
			self.model = WhisperModel(model_name, compute_type=compute_type)
			self.transcribe = self._transcribe_faster_whisper
			self.name = ':'.join([bk_name, model_name, compute_type])
		elif bk_name == 'whisper':
			import whisper
			model_name = model_name or 'base'
			self.model = whisper.load_model(model_name, in_memory=True)
			self.transcribe = self._transcribe_whisper
			self.name = ':'.join([bk_name, model_name])
		elif bk_name == 'qwen':
			import torch
			from langcodes import find as LC_find
			from qwen_asr import Qwen3ASRModel
			model_name = model_name or "Qwen/Qwen3-ASR-1.7B"
			self.model = Qwen3ASRModel.from_pretrained(model_name, dtype=torch.float16, device_map="cuda", max_inference_batch_size=1, max_new_tokens=1024)
			self.language_from_name = lambda name: _try(lambda: LC_find(name).language, name)
			self.transcribe = self._transcribe_qwen
			self.name = ':'.join([bk_name, model_name])
		else:
			self.name = compo_name
			if verbose:
				print(f'Unknown ASR model {compo_name}, offline ASR model not loaded', file=sys.stderr)
			return
		if verbose:
			print(f'Offline ASR model `{self.name}` loaded successfully ...', file=sys.stderr)

	def __bool__(self):
		return hasattr(self, 'model')

	def transcribe(self, filepath):
		return {}

	def _transcribe_whisper(self, filepath):
		return self.model.transcribe(os.path.expanduser(filepath))

	def _transcribe_faster_whisper(self, filepath):
		segs, info = self.model.transcribe(os.path.expanduser(filepath))
		txt = ' '.join([seg.text for seg in segs])
		return {'text': txt, 'language': info.language}

	def _transcribe_qwen(self, filepath):
		obj = self.model.transcribe(audio=os.path.expanduser(filepath), language=None)[0]
		return {'text': obj.text, 'language': self.language_from_name(obj.language)}


def _try(*args):
	exc = None
	for arg in args:
		try:
			return arg() if callable(arg) else arg
		except Exception as e:
			exc = e
	return exc
