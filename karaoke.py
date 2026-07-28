import os, sys, io, random, time, json, datetime
import logging, socket, subprocess, threading
import multiprocessing as mp
import shutil, psutil, traceback
import configparser
from subprocess import check_output
from collections import *

import numpy as np

from constants import media_types

import pygame
import qrcode
import arabic_reshaper
from bidi.algorithm import get_display
from unidecode import unidecode
from flask import request
from lib import omxclient, vlcclient
from lib.get_platform import *
from lib.NLP import *
from app import getString

if get_platform() != "windows":
	from signal import SIGALRM, alarm, signal, SIGTERM
	signal(SIGTERM, lambda signum, stack_frame: os.K.stop())

STD_VOL = 65536/8/np.sqrt(2)
ip2websock, ip2pane = {}, {}

ws_send = lambda ip, msg: ip2websock[ip].send(msg) if ip in ip2websock else None

def flash(message: str, category: str = "message", client_ip = ''):
	ws_send(client_ip or request.remote_addr, f'showNotification("{message}", "{category}")')

def cleanse_modules(name):
	try:
		for module_name in sorted(sys.modules.keys()):
			if module_name.startswith(name):
				del sys.modules[module_name]
		del globals()[name]
	except:
		pass


class Karaoke:
	ref_W, ref_H = 1920, 1080      # reference screen size, control drawing scale

	queue = []
	queue_json = ''
	available_songs = []
	rename_history = {}
	songname_trans = {} # transliteration is used for sorting and initial letter search
	now_playing = None
	now_playing_filename = None
	now_playing_user = None
	now_playing_transpose = 0
	now_playing_slave = ''
	audio_delay = 0
	has_video = True
	has_subtitle = False
	subtitle_delay = 0
	default_subtitle_delay = -3/4
	play_speed = 1.0
	show_subtitle = True
	last_vocal_info = 0
	last_vocal_time = 0
	use_DNN_vocal = True
	normalize_vol = False
	run_vocal = False
	vocal_process = None
	vocal_device = None
	vocal_mode = 'mixed'
	is_paused = True
	firstSongStarted = False
	switchingSong = False
	qr_code_path = None
	base_path = os.path.dirname(__file__)
	volume_offset = 0
	default_logo_path = os.path.join(base_path, "logo.jpg")
	logical_volume = None   # for normalized volume
	status_dirty = True
	event_dirty = threading.Event()

	def __init__(self, args):

		# override with supplied constructor args if provided
		self.__dict__.update(args.__dict__)
		self.omxplayer_adev = 'both'
		self.download_path = args.dl_path
		self.volume_offset = self.volume = args.volume
		self.logo_path = self.default_logo_path if args.logo_path == None else args.logo_path
		self.subtitle_delay = self.default_subtitle_delay

		# other initializations
		self.platform = get_platform()
		self.vlcclient = None
		self.omxclient = None
		self.screen = None
		self.player_state = {}
		self.downloading_songs = {}
		self.download_jobs = {}
		self.pending_enqueued_downloads = 0
		self.download_lock = threading.RLock()
		self.best_quality_active = set()
		self.accept_best_quality_work = True
		self.best_quality_work_lock = threading.Lock()
		self.best_quality_deferred_vocal_restart = False
		self.active_best_quality_jobs = 0
		self.quality_lock = threading.RLock()
		self.quality_metadata = {}
		self.quality_metadata_dirty = False
		self.best_quality_scan_interval = 30
		self.log_level = int(args.log_level)

		logging.basicConfig(
			format = "[%(asctime)s] %(levelname)s: %(message)s",
			datefmt = "%Y-%m-%d %H:%M:%S",
			level = self.log_level,
		)

		logging.debug(vars(args))

		self.quality_metadata_path = self.download_path + '.quality.json'
		self.load_quality_metadata()

		if self.save_delays:
			self.init_save_delays()

		self.load_config()

		# Generate connection URL and QR code, retry in case pi is still starting up
		# and doesn't have an IP yet (occurs when launched from /etc/rc.local)
		end_time = int(time.time()) + 30

		if self.platform == "raspberry_pi":
			while int(time.time()) < end_time:
				addresses_str = check_output(["hostname", "-I"]).strip().decode("utf-8")
				addresses = addresses_str.split(" ")
				self.ip = addresses[0]
				if not self.is_network_connected():
					logging.debug("Couldn't get IP, retrying....")
				else:
					break
		else:
			self.ip = self.get_ip()

		logging.debug("IP address (for QR code and splash screen): " + self.ip)

		self.url = "%s://%s:%s" % (('https' if self.ssl else 'http'), self.ip, self.port)

		# get songs from download_path
		self.get_available_songs()
		self.get_youtubedl_version()
		self.song2vol = Try(lambda: json.load(Open(self.download_path+'/.mp3_volume.json.gz')), {})
		
		# Automatically upgrade yt-dlp if using pip
		if not args.youtubedl_path:
			threading.Thread(target=self._upgrade_yt_dlp).start()

		# clean up old sessions
		self.kill_player()

		self.generate_qr_code()
		if self.use_vlc:
			self.vlcclient = vlcclient.VLCClient(port = self.vlc_port, path = self.vlc_path,
			                                     qrcode = (self.qr_code_path if self.show_overlay else None), url = self.url)
		else:
			self.omxclient = omxclient.OMXClient(path = self.omxplayer_path, adev = self.omxplayer_adev,
			                                     dual_screen = self.dual_screen, volume_offset = self.volume_offset)

		if not self.hide_splash_screen:
			self.initialize_screen(not args.windowed)
			self.render_splash_screen()

	def _upgrade_yt_dlp(self):
		import pip, yt_dlp
		fn = '.yt-dlp.last-update'
		date_today = datetime.datetime.today().isoformat()[:10]
		date_last = Try(lambda: open(fn).read().strip(), '')
		if date_today == date_last:
			logging.info(f"yt-dlp is up-to-date at {date_today}")
			return

		self.upgrade_youtubedl()
		self.get_youtubedl_version()
		with open(fn, 'w') as fp:
			print(date_today, file=fp)

	# Other ip-getting methods are unreliable and sometimes return 127.0.0.1
	# https://stackoverflow.com/a/28950776
	def get_ip(self):
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		try:
			# doesn't even have to be reachable
			s.connect(("8.8.8.8", 1))
			IP = s.getsockname()[0]
		except Exception:
			IP = "127.0.0.1"
		finally:
			s.close()
		return IP

	def get_youtubedl_version(self):
		self.youtubedl_version = self.call_yt_dlp(['--version'], True).strip()
		return self.youtubedl_version

	def upgrade_youtubedl(self):
		logging.info("Upgrading youtube-dl, current version: %s" % self.youtubedl_version)
		if self.youtubedl_path:
			self.call_yt_dlp(['-U'])
		else:
			try:
				import pip
				pip.main(['install', 'yt-dlp', '-U'])
				cleanse_modules('yt_dlp')
				import yt_dlp
			except:
				pass
		logging.info("Done. New version: %s" % self.get_youtubedl_version())

	def is_network_connected(self):
		return not len(self.ip) < 7

	def generate_qr_code(self):
		logging.debug("Generating URL QR code")
		qr = qrcode.QRCode(version = 1, box_size = 1, border = 4, error_correction = qrcode.constants.ERROR_CORRECT_H)
		qr.add_data(self.url)
		qr.make()
		img = qr.make_image()
		self.qr_code_path = os.path.join(self.base_path, "qrcode.png")
		img.save(self.qr_code_path)

	def get_default_display_mode(self):
		if self.use_vlc:
			if self.platform == "raspberry_pi":
				# HACK apparently if display mode is fullscreen the vlc window will be at the bottom of pygame
				os.environ["SDL_VIDEO_CENTERED"] = "1"
				return pygame.NOFRAME
			else:
				return pygame.FULLSCREEN
		else:
			return pygame.FULLSCREEN

	def initialize_screen(self, fullscreen=True):
		if not self.hide_splash_screen:
			logging.debug("Initializing pygame")
			os.environ['SDL_VIDEO_MAC_FULLSCREEN_SPACES'] = '0'
			pygame.init()
			pygame.display.set_caption("pikaraoke")
			pygame.mouse.set_visible(0)
			self.fonts = {}
			self.WIDTH = pygame.display.Info().current_w
			self.HEIGHT = pygame.display.Info().current_h
			logging.debug("Initializing screen mode")

			if self.platform != "raspberry_pi":
				self.toggle_full_screen(fullscreen)
			else:
				# this section is an unbelievable nasty hack - for some reason Pygame
				# needs a keyboardinterrupt to initialise in some limited circumstances
				# source: https://stackoverflow.com/questions/17035699/pygame-requires-keyboard-interrupt-to-init-display
				class Alarm(Exception):
					pass

				def alarm_handler(signum, frame):
					raise Alarm

				signal(SIGALRM, alarm_handler)
				alarm(3)
				try:
					self.toggle_full_screen(fullscreen)
					alarm(0)
				except Alarm:
					raise KeyboardInterrupt
			logging.debug("Done initializing splash screen")

	def toggle_full_screen(self, fullscreen=None):
		if not self.hide_splash_screen:
			logging.debug("Toggling fullscreen...")
			self.full_screen = not self.full_screen if fullscreen is None else fullscreen
			if self.full_screen:
				self.screen = pygame.display.set_mode([self.WIDTH, self.HEIGHT], self.get_default_display_mode())
			else:
				self.screen = pygame.display.set_mode([self.WIDTH*3//4, self.HEIGHT*3//4], pygame.RESIZABLE)
			if self.is_file_playing():
				self.play_transposed(self.now_playing_transpose)
			else:
				self.render_splash_screen()

	def normalize(self, v):
		r = self.screen.get_width()/self.ref_W
		if type(v) is list:
			return [i*r for i in v]
		elif type(v) is tuple:
			return tuple(i * r for i in v)
		return v*r

	def render_splash_screen(self):
		if self.hide_splash_screen:
			return

		# Clear the screen and start
		logging.debug("Rendering splash screen")
		self.screen.fill((0, 0, 0))
		blitY = self.ref_W*self.screen.get_height()//self.screen.get_width() - 40
		sysfont_size = 30

		# Draw logo and name
		text = self.render_font(sysfont_size * 2, getString(136), (255, 255, 255))
		if not hasattr(self, 'logo'):
			self.logo = pygame.image.load(self.logo_path)
		_, _, W, H = self.normalize(list(self.logo.get_rect()))
		W, H = W/2, H/2
		center = self.screen.get_rect().center
		self.logo1 = pygame.transform.scale(self.logo, (W, H))
		self.screen.blit(self.logo1, (center[0]-W/2, center[1]-H/2-text[1].height/2))
		self.screen.blit(text[0], (center[0]-text[1].width/2, center[1]+H/2))

		if not self.hide_ip:
			qr_size = 150
			if not hasattr(self, 'p_image'):
				self.p_image = pygame.image.load(self.qr_code_path)
			self.p_image1 = pygame.transform.scale(self.p_image, self.normalize((qr_size, qr_size)))
			self.screen.blit(self.p_image1, self.normalize((20, blitY - 125)))
			if not self.is_network_connected():
				text = self.render_font(sysfont_size, getString(48), (255, 255, 255))
				self.screen.blit(text[0], self.normalize((qr_size + 35, blitY)))
				time.sleep(10)
				logging.info("No IP found. Network/Wifi configuration required. For wifi config, try: sudo raspi-config or the desktop GUI: startx")
				self.stop()
			else:
				text = self.render_font(sysfont_size, getString(49) + self.url, (255, 255, 255))
				self.screen.blit(text[0], self.normalize((qr_size + 35, blitY)))
				# Windows and Mac-OS should use screen projection and AirPlay
				if self.streamer_alive():
					text = self.render_font(sysfont_size, getString(50) + self.url.rsplit(":", 1)[0] + ":4000", (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 40)))
				if not self.firstSongStarted:
					text = self.render_font(sysfont_size, getString(51), (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 120)))
					text = self.render_font(sysfont_size, getString(52), (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 80)))

		blitY = 10
		if not self.has_video:
			logging.debug("Rendering current song to splash screen")
			render_next_song = self.render_font([60, 50, 40], getString(58) + (self.now_playing or ''), (255, 255, 0))
			render_next_user = self.render_font([50, 40, 30], getString(57) + (self.now_playing_user or ''), (0, 240, 0))
			self.screen.blit(render_next_song[0], (self.screen.get_width() - render_next_song[1].width - 10, self.normalize(10)))
			self.screen.blit(render_next_user[0], (self.screen.get_width() - render_next_user[1].width - 10, self.normalize(80)))
			blitY += 140

		if len(self.queue) >= 1:
			logging.debug("Rendering next song to splash screen")
			next_song = self.queue[0]["title"]
			next_user = self.queue[0]["user"]
			render_next_song = self.render_font([60, 50, 40], getString(56) + next_song, (255, 255, 0))
			render_next_user = self.render_font([50, 40, 30], getString(57) + next_user, (0, 240, 0))
			self.screen.blit(render_next_song[0], (self.screen.get_width() - render_next_song[1].width - 10, self.normalize(blitY)))
			self.screen.blit(render_next_user[0], (self.screen.get_width() - render_next_user[1].width - 10, self.normalize(blitY+70)))
		elif not self.firstSongStarted:
			text1 = self.render_font(sysfont_size, getString(196) + ': ' + self.download_path, (255, 255, 0))
			self.screen.blit(text1[0], self.normalize((20, 20)))
			text2 = self.render_font(sysfont_size, getString(197) + ': %d'%len(self.available_songs), (255, 255, 0))
			self.screen.blit(text2[0], self.normalize((20, 30+sysfont_size)))

	def render_font(self, sizes, text, *kargs):
		if type(sizes) != list:
			sizes = [sizes]

		# normalize font size
		sizes = [s*self.screen.get_width()/self.ref_W for s in sizes]

		# initialize fonts if not found
		for size in sizes:
			if size not in self.fonts:
				self.fonts[size] = [pygame.freetype.SysFont(pygame.freetype.get_default_font(), size)] \
						+ [pygame.freetype.Font(f'font/{name}', size) for name in ['arial-unicode-ms.ttf', 'unifont.ttf']]

		# find a font that contains all characters of the song title, if cannot find, then display transliteration instead
		found = None
		for ii, font in enumerate(self.fonts[size]):
			if None not in font.get_metrics(text):
				found = ii
				break
		if found is None:
			text = unidecode(text)
			found = 0

		# reshape Arabic text
		text = get_display(arabic_reshaper.reshape(text))

		# draw the font, if too wide, half the string
		width = self.screen.get_width()
		for size in sorted(sizes, reverse = True):
			font = self.fonts[size][found]
			render = font.render(text, *kargs)
			# reduce font size if text too long
			if render[1].width > width and size != min(sizes):
				continue
			while render[1].width >= width:
				text = text[:int(len(text) * min(width / render[1].width, 0.618))] + '…'
				del render
				render = font.render(text, *kargs)
			break
		return render

	def call_yt_dlp(self, argv, get_stdout = False):
		if self.youtubedl_path:
			if get_stdout:
				return subprocess.check_output([self.youtubedl_path]+argv).decode("utf-8")
			else:
				return subprocess.call([self.youtubedl_path]+argv)
		ret_code = 0
		if get_stdout:
			old_stdout = sys.stdout
			sys.stdout = io.StringIO()
		try:
			import yt_dlp
			yt_dlp.main(argv)
		except SystemExit as e:
			ret_code = e.code
		if get_stdout:
			ret_stdout = sys.stdout
			sys.stdout = old_stdout
			return ret_stdout.getvalue()
		return ret_code

	def get_search_results(self, textToSearch):
		logging.info("Searching YouTube for: " + textToSearch)
		num_results = 10
		yt_search = 'ytsearch%d:%s' % (num_results, textToSearch)
		cmd = ["-j", "--no-playlist", "--flat-playlist", yt_search]
		logging.debug("Youtube-dl search command: " + " ".join(cmd))
		try:
			# output = subprocess.check_output(cmd).decode("utf-8")
			output = self.call_yt_dlp(cmd, True)
			logging.debug("Search results: " + output)
			rc = []
			for each in output.split("\n"):
				if len(each) > 2:
					j = json.loads(each)
					if (not "title" in j) or (not "url" in j):
						continue
					rc.append([j["title"], j["url"], j["id"], sec2hhmmss(j["duration"])])
			return rc
		except Exception as e:
			logging.debug("Error while executing search: " + str(e))
			raise e

	def get_yt_dlp_json(self, url, extra_opts=None):
		out_json = self.call_yt_dlp(['-j', '--remote-components', 'ejs:github'] + (extra_opts or []) + [url], True)
		try:
			return json.loads(out_json)
		except json.JSONDecodeError as e:
			raise RuntimeError(f'yt-dlp did not return video metadata for {url}') from e

	def get_youtube_id_from_path(self, song_path):
		stem = os.path.splitext(os.path.basename(song_path))[0]
		return stem.rsplit('---', 1)[1] if '---' in stem else None

	def youtube_url_from_id(self, youtube_id):
		return f'https://www.youtube.com/watch?v={youtube_id}'

	def load_quality_metadata(self):
		try:
			if os.path.isfile(self.quality_metadata_path):
				with open(self.quality_metadata_path) as fp:
					self.quality_metadata = json.load(fp)
			else:
				self.quality_metadata = {}
		except Exception as e:
			logging.warning(f"Could not load quality metadata: {e}")
			self.quality_metadata = {}

	def save_quality_metadata(self):
		with self.quality_lock:
			if not self.quality_metadata_dirty:
				return
			tmp = self.quality_metadata_path + '.tmp'
			try:
				with open(tmp, 'w') as fp:
					json.dump(self.quality_metadata, fp, indent=1, sort_keys=True)
				shutil.move(tmp, self.quality_metadata_path)
				self.quality_metadata_dirty = False
			except Exception as e:
				logging.warning(f"Could not save quality metadata: {e}")

	def ffprobe_json(self, filename):
		out = subprocess.check_output([
			'ffprobe', '-v', 'error',
			'-show_entries', 'format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,pix_fmt,color_range,color_space,color_transfer,color_primaries',
			'-of', 'json', filename
		], stderr=subprocess.STDOUT).decode('utf-8')
		return json.loads(out)

	def probe_media(self, filename):
		try:
			info = self.ffprobe_json(filename)
			video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), {})
			audio = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), {})
			duration = float(info.get('format', {}).get('duration') or 0)
			filesize = int(info.get('format', {}).get('size') or os.path.getsize(filename))
			return {
				'path': os.path.basename(filename),
				'height': video.get('height'),
				'width': video.get('width'),
				'duration': duration,
				'filesize': filesize,
				'container': os.path.splitext(filename)[1].lstrip('.').lower(),
				'video_codec': video.get('codec_name'),
				'audio_codec': audio.get('codec_name'),
				'pix_fmt': video.get('pix_fmt'),
				'color_range': video.get('color_range'),
				'color_space': video.get('color_space'),
				'color_transfer': video.get('color_transfer'),
				'color_primaries': video.get('color_primaries'),
			}
		except Exception as e:
			logging.debug(f"Could not probe media {filename}: {e}")
			return {}

	def update_quality_metadata_for_file(self, filename, quality_profile=None, best_available=None, extra=None):
		youtube_id = self.get_youtube_id_from_path(filename)
		if not youtube_id or not os.path.isfile(filename):
			return
		probe = self.probe_media(filename)
		with self.quality_lock:
			meta = self.quality_metadata.get(youtube_id, {})
			meta.update(probe)
			meta.update({
				'youtube_id': youtube_id,
				'title': self.filename_from_path(filename),
				'checked_at': datetime.datetime.now().astimezone().isoformat(),
			})
			if quality_profile:
				meta['quality_profile'] = quality_profile
			if best_available is not None:
				meta['best_available'] = best_available
			if extra:
				meta.update(extra)
			self.quality_metadata[youtube_id] = meta
			self.quality_metadata_dirty = True
			self.status_dirty = True
		self.save_quality_metadata()

	def get_quality_label(self, song_path):
		youtube_id = self.get_youtube_id_from_path(song_path)
		meta = self.quality_metadata.get(youtube_id, {}) if youtube_id else {}
		height = meta.get('height')
		if height:
			label = f"{height}p"
		elif meta.get('video_codec'):
			label = 'video'
		else:
			label = 'unknown'
		if meta.get('upgrade_state') == 'upgrading':
			return 'Upgrading'
		if meta.get('upgrade_state') == 'failed':
			return f"{label} retry later"
		if meta.get('best_available'):
			return f"{label} best"
		if meta.get('quality_profile') == 'default':
			return f"{label} default"
		return label

	def get_quality_tag_class(self, song_path):
		youtube_id = self.get_youtube_id_from_path(song_path)
		meta = self.quality_metadata.get(youtube_id, {}) if youtube_id else {}
		if meta.get('upgrade_state') == 'upgrading':
			return 'is-warning'
		if meta.get('upgrade_state') == 'failed':
			return 'is-danger is-light'
		if meta.get('best_available'):
			return 'is-success is-light'
		if meta.get('quality_profile') == 'default':
			return 'is-info is-light'
		return 'is-dark is-light'

	def get_quality_status_payload(self, song_path):
		return {
			'label': self.get_quality_label(song_path),
			'className': self.get_quality_tag_class(song_path),
		}

	def set_quality_failure(self, youtube_id, reason):
		with self.quality_lock:
			meta = self.quality_metadata.get(youtube_id, {'youtube_id': youtube_id})
			failures = int(meta.get('failure_count', 0)) + 1
			delay = min(24 * 3600, 300 * (2 ** min(failures - 1, 6)))
			meta.update({
				'failure_count': failures,
				'last_failure': str(reason),
				'next_retry_at': (datetime.datetime.now().astimezone() + datetime.timedelta(seconds=delay)).isoformat(),
				'upgrade_state': 'failed',
			})
			self.quality_metadata[youtube_id] = meta
			self.quality_metadata_dirty = True
			self.status_dirty = True
		print(f"Best-quality upgrade failed for {youtube_id}: {reason}", flush=True)
		self.save_quality_metadata()

	def clear_quality_failure(self, youtube_id):
		with self.quality_lock:
			meta = self.quality_metadata.get(youtube_id, {'youtube_id': youtube_id})
			meta['failure_count'] = 0
			meta['next_retry_at'] = None
			meta.pop('last_failure', None)
			meta.pop('upgrade_state', None)
			self.quality_metadata[youtube_id] = meta
			self.quality_metadata_dirty = True
			self.status_dirty = True
		self.save_quality_metadata()

	def is_retry_ready(self, youtube_id):
		meta = self.quality_metadata.get(youtube_id, {})
		next_retry = meta.get('next_retry_at')
		if not next_retry:
			return True
		try:
			return datetime.datetime.fromisoformat(next_retry) <= datetime.datetime.now().astimezone()
		except:
			return True

	def ffmpeg_null_scan(self, filename):
		return subprocess.call(['ffmpeg', '-v', 'error', '-t', '10', '-i', filename, '-f', 'null', '-']) == 0

	def best_quality_cookie_opts(self):
		opts = []
		skip_next = False
		for idx, opt in enumerate(self.cookies_opt):
			if skip_next:
				skip_next = False
				continue
			if opt == '--extractor-args' and idx + 1 < len(self.cookies_opt) and 'player_client=web' in self.cookies_opt[idx + 1]:
				skip_next = True
				continue
			opts.append(opt)
		return opts

	def get_best_expected_height(self, info):
		heights = []
		for fmt in info.get('formats') or []:
			if fmt.get('vcodec') in [None, 'none']:
				continue
			height = fmt.get('height')
			if isinstance(height, int):
				heights.append(height)
		return min(max(heights or [0]), 2160)

	def ytdlp_subprocess_cmd(self, argv):
		if self.youtubedl_path:
			return [self.youtubedl_path] + argv
		return [sys.executable, '-m', 'yt_dlp'] + argv

	def call_yt_dlp_subprocess(self, argv, job=None):
		cmd = self.ytdlp_subprocess_cmd(argv)
		logging.info("Youtube-dl command: " + " ".join(cmd))
		p = subprocess.Popen(cmd)
		if job is not None:
			job['process'] = p
		while True:
			rc = p.poll()
			if rc is not None:
				return rc
			if job is not None and (job.get('cancel') or self.should_cancel_best_job(job)):
				logging.info(f"Canceling best-quality job for {job.get('old_path') or job.get('url')}")
				p.terminate()
				try:
					return p.wait(timeout=10)
				except subprocess.TimeoutExpired:
					p.kill()
					return p.wait()
			time.sleep(1)

	def should_cancel_best_job(self, job):
		if job.get('quality') != 'best':
			return False
		old_path = job.get('old_path')
		if not old_path:
			return False
		if old_path == self.now_playing_filename:
			return True
		if old_path not in self.queued_protected_paths():
			return False
		return time.time() - job.get('started_at', time.time()) < self.best_quality_cancel_threshold

	def cancel_protected_best_jobs(self):
		with self.download_lock:
			jobs = list(self.download_jobs.values())
		for job in jobs:
			if self.should_cancel_best_job(job):
				job['cancel'] = True
				proc = job.get('process')
				if proc and proc.poll() is None:
					proc.terminate()

	def estimated_converted_size(self, duration):
		return int(max(duration or 0, 1) * (20_000_000 + 192_000) / 8)

	def has_best_quality_space(self, old_path=None, duration=0, source_size=0):
		usage = shutil.disk_usage(self.download_path)
		old_size = os.path.getsize(old_path) if old_path and os.path.isfile(old_path) else 0
		required = source_size + self.estimated_converted_size(duration) + old_size + 2 * 1024**3
		if source_size == 0 and duration == 0:
			required = max(10 * 1024**3, int(3.5 * old_size))
		return usage.free >= required

	def find_downloaded_media_file(self, directory):
		files = []
		for bn in os.listdir(directory):
			fn = os.path.join(directory, bn)
			if os.path.isfile(fn) and os.path.splitext(fn)[1].lower() in media_types:
				files.append(fn)
		return max(files, key=os.path.getsize) if files else None

	def find_srt_sidecar(self, directory):
		for bn in os.listdir(directory):
			fn = os.path.join(directory, bn)
			if os.path.isfile(fn) and os.path.splitext(fn)[1].lower() == '.srt':
				return fn
		return None

	def normalize_subtitle_lang(self, lang):
		if not lang:
			return None
		lang = str(lang).strip().replace('_', '-')
		if not lang:
			return None
		return lang

	def subtitle_language_candidates(self, lang):
		lang = self.normalize_subtitle_lang(lang)
		if not lang:
			return []
		parts = [part for part in lang.split(',') if part.strip()]
		lang = self.normalize_subtitle_lang(parts[0] if parts else lang)
		base = lang.split('-', 1)[0].lower()
		candidates = [lang]
		if base and base != lang.lower():
			candidates.append(base)
		return list(dict.fromkeys([i for i in candidates if i]))

	def subtitle_lang_rank(self, lang, targets, auto=False):
		lang_lower = (lang or '').lower()
		for target_index, target in enumerate(targets or []):
			target = self.normalize_subtitle_lang(target)
			if not target:
				continue
			target_lower = target.lower()
			target_base = target_lower.split('-', 1)[0]
			offset = target_index * 10
			if lang_lower == target_lower:
				return offset
			if auto and lang_lower == target_base + '-orig':
				return offset + 1
			if lang_lower == target_base:
				return offset + 2
			if lang_lower.startswith(target_base + '-') or lang_lower.startswith(target_base + '_'):
				return offset + 3
		return None

	def choose_matching_subtitle_lang(self, subtitles, targets, auto=False):
		candidates = []
		for lang in (subtitles or {}).keys():
			rank = self.subtitle_lang_rank(lang, targets, auto)
			if rank is not None:
				candidates.append((rank, lang))
		return sorted(candidates)[0][1] if candidates else None

	def best_quality_subtitle_targets(self, info):
		targets = []
		for key in ['language', 'original_language']:
			for lang in self.subtitle_language_candidates(info.get(key)):
				targets.append(lang)
		return list(dict.fromkeys(targets))

	def best_quality_subtitle_options(self, info):
		targets = self.best_quality_subtitle_targets(info)
		manual_lang = self.choose_matching_subtitle_lang(info.get('subtitles'), targets, auto=False)
		if not manual_lang and not targets and len(info.get('subtitles') or {}) == 1:
			manual_lang = next(iter(info.get('subtitles') or {}))
		if manual_lang:
			return [
				'--sub-langs', manual_lang,
				'--write-subs',
				'--sub-format', 'srt/vtt/best',
				'--convert-subs', 'srt',
			], {'subtitle_language': manual_lang, 'subtitle_source': 'manual'}
		auto_lang = self.choose_matching_subtitle_lang(info.get('automatic_captions'), targets, auto=True)
		if auto_lang:
			return [
				'--sub-langs', auto_lang,
				'--write-auto-subs',
				'--sub-format', 'srt/vtt/best',
				'--convert-subs', 'srt',
			], {'subtitle_language': auto_lang, 'subtitle_source': 'auto'}
		return [], {'subtitle_language': None, 'subtitle_source': None}

	def ffmpeg_color_args(self, probe):
		args = []
		for flag, key in [
			('-color_range', 'color_range'),
			('-colorspace', 'color_space'),
			('-color_trc', 'color_transfer'),
			('-color_primaries', 'color_primaries'),
		]:
			val = probe.get(key)
			if val and val != 'unknown':
				args += [flag, val]
		return args

	def ffmpeg_pixel_args(self, probe):
		pix_fmt = probe.get('pix_fmt') or ''
		color_transfer = probe.get('color_transfer') or ''
		if '10' in pix_fmt or color_transfer in ['smpte2084', 'arib-std-b67']:
			return ['-pix_fmt', 'p010le', '-profile:v', 'main10']
		return ['-pix_fmt', 'yuv420p']

	def convert_best_source_to_mp4(self, source_path, final_path):
		probe = self.probe_media(source_path)
		base = [
			'ffmpeg', '-y',
			'-i', source_path,
			'-map', '0:v:0', '-map', '0:a:0?', '-map', '0:s?',
			'-c:v', 'hevc_videotoolbox',
			'-b:v', '20M',
			'-maxrate', '28M',
			'-bufsize', '56M',
			'-tag:v', 'hvc1',
		] + self.ffmpeg_pixel_args(probe) + self.ffmpeg_color_args(probe) + [
			'-c:a', 'aac',
			'-b:a', '192k',
			'-c:s', 'mov_text',
			final_path
		]
		logging.info("FFmpeg conversion command: " + " ".join(base))
		rc = subprocess.call(base)
		if rc == 0:
			return True

		logging.warning("FFmpeg conversion with embedded subtitles failed; retrying without embedded subtitles")
		cmd = [
			'ffmpeg', '-y',
			'-i', source_path,
			'-map', '0:v:0', '-map', '0:a:0?',
			'-c:v', 'hevc_videotoolbox',
			'-b:v', '20M',
			'-maxrate', '28M',
			'-bufsize', '56M',
			'-tag:v', 'hvc1',
		] + self.ffmpeg_pixel_args(probe) + self.ffmpeg_color_args(probe) + [
			'-c:a', 'aac',
			'-b:a', '192k',
			final_path
		]
		logging.info("FFmpeg conversion command: " + " ".join(cmd))
		return subprocess.call(cmd) == 0

	def verify_best_output(self, filename, expected_height=None):
		probe = self.probe_media(filename)
		if not probe or not probe.get('height') or probe.get('video_codec') not in ['hevc', 'h265']:
			return False
		if expected_height and int(probe.get('height') or 0) < expected_height:
			logging.warning(f"Converted best output height {probe.get('height')} is below expected height {expected_height}")
			return False
		return self.ffmpeg_null_scan(filename)

	def best_quality_source_dir(self, youtube_id):
		path = os.path.join(self.tmp_dir, f'best-{youtube_id}-{int(time.time())}')
		os.makedirs(path, exist_ok=True)
		return path

	def download_best_quality_file(self, song_url, old_path=None, job=None):
		best_opts = self.best_quality_cookie_opts()
		info = self.get_yt_dlp_json(song_url, best_opts)
		youtube_id = info['id']
		duration = float(info.get('duration') or 0)
		expected_height = self.get_best_expected_height(info)
		subtitle_opts, subtitle_extra = self.best_quality_subtitle_options(info)
		if not self.has_best_quality_space(old_path, duration):
			raise RuntimeError('not enough free disk space for best-quality download')

		work_dir = self.best_quality_source_dir(youtube_id)
		with self.quality_lock:
			self.active_best_quality_jobs += 1
		try:
			source_tpl = os.path.join(work_dir, '%(title)s---%(id)s.%(ext)s')
			fmt_best = 'bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b'
			cmd = [
				'--fixup', 'force', '--socket-timeout', '3', '-R', 'infinite',
				'--merge-output-format', 'mkv',
				'-f', fmt_best,
				'-S', 'res:2160,fps,hdr,vcodec,acodec,size',
				'-o', source_tpl,
			] + subtitle_opts + best_opts + [song_url]
			rc = self.call_yt_dlp_subprocess(cmd, job)
			if rc != 0:
				raise RuntimeError(f'yt-dlp best source download failed with code {rc}')

			source_path = self.find_downloaded_media_file(work_dir)
			if not source_path:
				raise RuntimeError('yt-dlp did not produce a best source media file')
			source_probe = self.probe_media(source_path)
			source_height = int(source_probe.get('height') or 0)
			if expected_height and source_height < expected_height:
				raise RuntimeError(f'yt-dlp selected {source_height}p, expected {expected_height}p best available')
			if not self.ffmpeg_null_scan(source_path):
				raise RuntimeError('best source failed 10-second decode scan')

			stem = os.path.splitext(os.path.basename(old_path or source_path))[0]
			final_path = os.path.join(work_dir, stem + '.converted.mp4')
			if not self.convert_best_source_to_mp4(source_path, final_path):
				raise RuntimeError('best source conversion failed')
			if not self.verify_best_output(final_path, expected_height):
				raise RuntimeError('converted best output failed verification')

			sidecar = self.find_srt_sidecar(work_dir)
			quality_extra = {
				'youtube_id': youtube_id,
				'best_expected_height': expected_height,
				'source_height': source_height,
				'source_video_codec': source_probe.get('video_codec'),
				'conversion_encoder': 'hevc_videotoolbox',
				'format_id': '+'.join([f.get('format_id', '') for f in info.get('requested_formats', [])]) if info.get('requested_formats') else info.get('format_id'),
			}
			quality_extra.update(subtitle_extra)
			return final_path, sidecar, work_dir, youtube_id, quality_extra
		except:
			shutil.rmtree(work_dir, ignore_errors=True)
			raise
		finally:
			with self.quality_lock:
				self.active_best_quality_jobs = max(0, self.active_best_quality_jobs - 1)

	def get_downloaded_file_basename(self, url):
		try:
			youtube_id = url.split("watch?v=")[1].split('&')[0]
		except:
			try:
				info_json = self.get_yt_dlp_json(url)
				youtube_id = info_json['id']
			except:
				logging.error("Error parsing video id from url: " + url)
				return None

		try:
			return [i for i in os.listdir(self.tmp_dir) if youtube_id in i][0]
		except:
			pass

		try:
			info_json = self.get_yt_dlp_json(url)
			filename = f"{info_json['title']}---{info_json['id']}.{info_json['ext']}"
			return filename if os.path.isfile(self.tmp_dir+'/'+filename) else None
		except:
			return None

	def queue_position_for_new_download(self, enqueue):
		return len(self.queue) + self.pending_enqueued_downloads + 1 if enqueue else 0

	def should_download_best_for_request(self, enqueue):
		pos = self.queue_position_for_new_download(enqueue)
		return bool(self.accept_best_quality_work and enqueue and pos >= 6)

	def move_sidecar_if_needed(self, sidecar, final_path):
		if not sidecar or not os.path.isfile(sidecar):
			return
		target = os.path.splitext(final_path)[0] + '.srt'
		try:
			shutil.move(sidecar, target)
		except Exception as e:
			logging.warning(f"Could not move subtitle sidecar {sidecar} to {target}: {e}")

	def delete_assoc_for_basename(self, basename):
		for fn in [
			self.download_path + 'nonvocal/' + basename + '.m4a',
			self.download_path + 'nonvocal/.' + basename + '.m4a',
			self.download_path + 'vocal/' + basename + '.m4a',
			self.download_path + 'vocal/.' + basename + '.m4a',
		]:
			self.delete_if_exist(fn)

	def stop_vocal_splitter_for_replacement(self, timeout=60):
		was_alive = bool(self.vocal_alive())
		if not was_alive:
			return False
		logging.info("Stopping vocal splitter before best-quality replacement")
		self.vocal_stop()
		deadline = time.time() + timeout
		while time.time() < deadline:
			if not self.vocal_alive():
				return True
			time.sleep(1)
		raise RuntimeError('vocal splitter did not stop before best-quality replacement')

	def replace_library_file_with_best(self, old_path, best_temp_path, sidecar=None, quality_extra=None, restart_vocal_after=True):
		old_basename = os.path.basename(old_path) if old_path else os.path.basename(best_temp_path)
		final_stem = os.path.splitext(old_basename)[0]
		if final_stem.endswith('.converted'):
			final_stem = final_stem[:-len('.converted')]
		final_basename = final_stem + '.mp4'
		final_path = self.download_path + final_basename
		backup_path = None
		should_stop_vocal = os.path.isfile(final_path) or bool(old_path and os.path.isfile(old_path))
		restart_vocal = self.stop_vocal_splitter_for_replacement() if should_stop_vocal else False
		replacement_complete = False

		try:
			if os.path.isfile(final_path):
				backup_path = final_path + '.old'
				self.delete_if_exist(backup_path)
				shutil.move(final_path, backup_path)
			elif old_path and os.path.isfile(old_path):
				backup_path = old_path + '.old'
				self.delete_if_exist(backup_path)
				shutil.move(old_path, backup_path)

			try:
				shutil.move(best_temp_path, final_path)
				self.move_sidecar_if_needed(sidecar, final_path)
			except:
				if backup_path and os.path.isfile(backup_path):
					shutil.move(backup_path, old_path or final_path)
				raise

			for item in self.queue:
				if item['file'] == old_path or item['file'] == backup_path:
					item['file'] = final_path
					item['title'] = self.filename_from_path(final_path)
			if self.now_playing_filename == old_path:
				self.now_playing_filename = final_path

			if backup_path:
				self.delete_if_exist(backup_path)
			if old_path and old_path != final_path:
				self.delete_if_exist(old_path)
			self.delete_assoc_for_basename(old_basename)
			self.get_available_songs()
			self.update_queue()
			self.update_quality_metadata_for_file(final_path, 'best', True, quality_extra)
			replacement_complete = True
			return final_path
		finally:
			if not restart_vocal_after and (replacement_complete or restart_vocal):
				self.best_quality_deferred_vocal_restart = True
			elif replacement_complete or restart_vocal:
				self.trigger_vocal_regeneration()

	def trigger_vocal_regeneration(self):
		Try(lambda: self.vocal_restart())

	def flush_deferred_vocal_regeneration(self):
		if self.best_quality_deferred_vocal_restart:
			self.best_quality_deferred_vocal_restart = False
			self.trigger_vocal_regeneration()

	def download_video(self, client_lang='', client_ip='', song_url = '', enqueue = False, song_added_by = "Pikaraoke", sub_langs = '', high_quality = False):
		logging.info("Downloading video: " + song_url)
		getString2 = lambda ii: os.langs.get(client_lang, os.langs['en_US'])[ii]
		self.downloading_songs[song_url] = 1
		with self.download_lock:
			job = {'url': song_url, 'started_at': time.time(), 'quality': 'best' if self.should_download_best_for_request(enqueue) else 'default'}
			if enqueue:
				self.pending_enqueued_downloads += 1
			self.download_jobs[song_url] = job
		if job['quality'] == 'best':
			work_dir = None
			try:
				with self.best_quality_work_lock:
					best_tmp, sidecar, work_dir, youtube_id, quality_extra = self.download_best_quality_file(song_url, job=job)
					final_path = self.replace_library_file_with_best(None, best_tmp, sidecar, quality_extra, restart_vocal_after=True)
				self.clear_quality_failure(youtube_id)
				if enqueue:
					self.enqueue(final_path, song_added_by)
					self.downloading_songs[song_url] = '00'
					flash(getString2(189)+' '+getString2(191), client_ip = client_ip)
				else:
					self.downloading_songs[song_url] = 0
					flash(getString2(189), client_ip = client_ip)
			except Exception as e:
				logging.error(f"Error downloading best-quality song {song_url}: {e}")
				try:
					info = self.get_yt_dlp_json(song_url)
					self.set_quality_failure(info.get('id', song_url), e)
				except:
					pass
				self.downloading_songs[song_url] = -1
				flash(getString2(190), client_ip = client_ip)
			finally:
				if work_dir:
					shutil.rmtree(work_dir, ignore_errors=True)
				with self.download_lock:
					if enqueue:
						self.pending_enqueued_downloads = max(0, self.pending_enqueued_downloads - 1)
					self.download_jobs.pop(song_url, None)
				ws_send(client_ip, 'download_ended()')
			return

		high_quality = False
		dl_path = "%(title)s---%(id)s.%(ext)s"
		fmt_hq  = 'bestvideo[height<=1080][vcodec^=h264]+bestaudio[acodec=aac]/bestvideo[height<=1080]+bestaudio'
		fmt_std = 'bestvideo[height<=720][vcodec^=h264]+bestaudio[acodec=aac]/bestvideo[height<=720]+bestaudio'
		opt_sub = ['--sub-langs', sub_langs, '--embed-subs', '--write-auto-subs', '--write-subs', '--sub-format', 'srt/vtt/best', '--convert-subs', 'srt'] if sub_langs else []
		base_opts = ['--fixup', 'force', '--socket-timeout', '3', '-R', 'infinite', '--remux-video', 'mp4']
		out_opt = ["-o", self.tmp_dir+'/'+dl_path]

		# Try requested quality first, fall back to standard, then no format constraint
		attempts = ([fmt_hq, fmt_std] if high_quality else [fmt_std]) + [None]
		rc = 1
		for fmt in attempts:
			opt_quality = ['-f', fmt] if fmt else []
			cmd = base_opts + self.cookies_opt + opt_quality + out_opt + opt_sub + [song_url]
			logging.info("Youtube-dl command: " + " ".join(cmd))
			rc = self.call_yt_dlp(cmd)
			if rc == 0:
				break
			logging.error(f"Download failed with format '{fmt}', trying next fallback ...")
		if rc == 0:
			logging.debug("Song successfully downloaded: " + song_url)
			self.downloading_songs[song_url] = 0
			bn = self.get_downloaded_file_basename(song_url)
			if bn:
				shutil.move(self.tmp_dir+'/'+bn, self.download_path+bn)
				self.update_quality_metadata_for_file(self.download_path+bn, 'default', False)
				self.get_available_songs()
				if enqueue:
					self.enqueue(self.download_path+bn, song_added_by)
					self.downloading_songs[song_url] = '00'
					flash(getString2(189)+' '+getString2(191), client_ip = client_ip)
				else:
					flash(getString2(189), client_ip = client_ip)
			else:
				logging.error("Error queueing song: " + song_url)
				self.downloading_songs[song_url] = '01'
				flash(getString2(189)+' '+getString2(192), client_ip = client_ip)
		else:
			logging.error("Error downloading song: " + song_url)
			self.downloading_songs[song_url] = -1
			flash(getString2(190), client_ip = client_ip)
		with self.download_lock:
			if enqueue:
				self.pending_enqueued_downloads = max(0, self.pending_enqueued_downloads - 1)
			self.download_jobs.pop(song_url, None)
		return ws_send(client_ip, 'download_ended()')

	def get_available_songs(self):
		logging.info("Fetching available songs in: " + self.download_path)
		files_grabbed = []
		self.songname_trans = {}
		for bn in os.listdir(self.download_path):
			fn = self.download_path + bn
			if not bn.startswith('.') and os.path.isfile(fn):
				if os.path.splitext(fn)[1].lower() in media_types:
					files_grabbed.append(fn)
					trans = unidecode(self.filename_from_path(fn)).lower()
					# strip leading non-transliterable symbols
					while trans and not trans[0].islower() and not trans[0].isdigit():
						trans = trans[1:]
					self.songname_trans[fn] = trans

		# self.available_songs = sorted(files_grabbed, key = lambda f: str.lower(os.path.basename(f)))
		self.available_songs = sorted(self.songname_trans, key = self.songname_trans.get)
		for fn in self.available_songs:
			youtube_id = self.get_youtube_id_from_path(fn)
			if youtube_id and youtube_id not in self.quality_metadata:
				self.update_quality_metadata_for_file(fn)

	def get_all_assoc_files(self, song_path):
		basename = os.path.basename(song_path)
		basestem = os.path.splitext(basename)
		return [self.download_path + basename,
				self.download_path + basestem[0] + '.srt',
				self.download_path + basestem[0] + '.cdg',
				self.download_path + 'nonvocal/' + basename + '.m4a',
				self.download_path + 'nonvocal/.' + basename + '.m4a',
				self.download_path + 'vocal/' + basename + '.m4a',
				self.download_path + 'vocal/.' + basename + '.m4a']

	def delete_if_exist(self, filename):
		if os.path.isfile(filename):
			try:
				os.remove(filename)
			except:
				pass

	def delete(self, song_path):
		logging.info("Deleting song: " + song_path)

		# delete all associated cdg/vocal/nonvocal files if exist
		for fn in self.get_all_assoc_files(song_path):
			self.delete_if_exist(fn)
		youtube_id = self.get_youtube_id_from_path(song_path)
		if youtube_id:
			with self.quality_lock:
				if youtube_id in self.quality_metadata:
					self.quality_metadata.pop(youtube_id)
					self.quality_metadata_dirty = True
			self.save_quality_metadata()

		self.get_available_songs()

	def rename_if_exist(self, old_path, new_path):
		if os.path.isfile(old_path):
			try:
				shutil.move(old_path, new_path)
			except:
				pass

	def rename(self, song_path, new_basestem):
		logging.info("Renaming song: '" + song_path + "' to: " + new_basestem)
		ext = os.path.splitext(song_path)
		if len(ext) < 2:
			ext += ['']
		new_basename = new_basestem + ext[1]

		# can handle the case while the file is being processed by vocal splitter, it has been renamed multiple times
		old_basename = os.path.basename(song_path)
		self.rename_history[old_basename] = new_basename
		for k, v in self.rename_history.items():
			if v == old_basename:
				self.rename_history[k] = new_basename

		# rename all associated cdg/vocal/nonvocal files if exist
		for src, tgt in zip(self.get_all_assoc_files(song_path), self.get_all_assoc_files(new_basename)):
			self.rename_if_exist(src, tgt)

		# rename queue entry if inside queue
		for item in self.queue:
			if item['file'] == song_path:
				item['file'] = self.download_path + new_basename
				item['title'] = self.filename_from_path(item['file'])
				break

		# migrate saved delays to new filename
		if self.save_delays and old_basename in self.delays:
			self.delays[new_basename] = self.delays.pop(old_basename)
			self.delays_dirty = True
			self.auto_save_delays()
		youtube_id = self.get_youtube_id_from_path(self.download_path + new_basename)
		if youtube_id and youtube_id in self.quality_metadata:
			with self.quality_lock:
				self.quality_metadata[youtube_id]['path'] = new_basename
				self.quality_metadata[youtube_id]['title'] = self.filename_from_path(new_basename)
				self.quality_metadata_dirty = True
			self.save_quality_metadata()

		self.get_available_songs()

	def filename_from_path(self, file_path):
		rc = os.path.basename(file_path)
		rc = os.path.splitext(rc)[0]
		rc = rc.split("---")[0]  # removes youtube id if present
		return rc

	def kill_player(self):
		if self.use_vlc:
			logging.debug("Killing old VLC processes")
			if self.vlcclient != None:
				self.vlcclient.kill()
		elif self.omxclient != None:
				self.omxclient.kill()

	def play_file(self, file_path, extra_params = []):
		self.switchingSong = True
		if self.use_vlc:
			if self.save_delays:
				saved_delays = self.delays.get(os.path.basename(file_path), {})
				self.audio_delay = self.audio_delay if self.audio_delay is not None else saved_delays.get('audio_delay', 0)
				self.subtitle_delay = saved_delays.get('subtitle_delay', self.default_subtitle_delay)
				self.show_subtitle = False if self.show_subtitle==False else saved_delays.get('show_subtitle', True)
			extra_params1 = []
			logging.info("Playing video in VLC: " + file_path)
			if self.platform != 'osx':
				extra_params1 += ['--drawable-hwnd' if self.platform == 'windows' else '--drawable-xid',
				                  hex(pygame.display.get_wm_info()['window'])]
			self.now_playing_slave = self.try_set_vocal_mode(self.vocal_mode, file_path)
			if os.path.isfile(self.now_playing_slave):
				extra_params1 += [f'--input-slave={self.now_playing_slave}', '--audio-track=1']
			if self.audio_delay:
				extra_params1 += [f'--audio-desync={self.audio_delay * 1000}']
			if self.subtitle_delay:
				extra_params1 += [f'--sub-delay={self.subtitle_delay * 10}']
			if self.show_subtitle:
				extra_params1 += [f'--sub-track=0']
			if self.play_speed != 1:
				extra_params1 += [f'--rate={self.play_speed}']
			self.now_playing = self.filename_from_path(file_path)
			self.now_playing_filename = file_path
			self.is_paused = ('--start-paused' in extra_params1)
			if self.normalize_vol and self.logical_volume is not None:
				self.volume = self.logical_volume / self.get_mp3_volume(file_path)
			if self.now_playing_transpose == 0:
				xml = self.vlcclient.play_file(file_path, self.volume, extra_params + extra_params1)
			else:
				xml = self.vlcclient.play_file_transpose(file_path, self.now_playing_transpose, self.volume, extra_params + extra_params1)
			self.has_subtitle = "<info name='Type'>Subtitle</info>" in xml
			if self.has_subtitle:
				if self.show_subtitle:
					self.vlcclient.enable_subtitle_track(xml)
				else:
					self.vlcclient.disable_subtitle_track()
			self.has_video = "<info name='Type'>Video</info>" in xml
			self.volume = round(float(self.vlcclient.get_val_xml(xml, 'volume')))
			if self.normalize_vol:
				self.media_vol = self.get_mp3_volume(self.now_playing_filename)
				self.logical_volume = self.volume * self.media_vol
		else:
			logging.info("Playing video in omxplayer: " + file_path)
			self.omxclient.play_file(file_path)

		self.switchingSong = False
		self.status_dirty = True
		self.render_splash_screen()  # remove old previous track

	def play_transposed(self, semitones):
		if self.use_vlc:
			self.now_playing_transpose = semitones
			status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
			info = self.vlcclient.get_info_xml(status_xml)
			posi = info['position']*info['length']
			self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))
		else:
			logging.error("Not using VLC. Can't transpose track.")

	def is_file_playing(self):
		client = self.vlcclient if self.use_vlc else self.omxclient
		if client is not None and client.is_running():
			return True
		elif self.now_playing_filename:
			self.now_playing = self.now_playing_filename = None
		return False

	def is_song_in_queue(self, song_path):
		return song_path in map(lambda t: t['file'], self.queue)

	def enqueue(self, song_path, user = "Pikaraoke"):
		if (self.is_song_in_queue(song_path)):
			logging.warn("Song is already in queue, will not add: " + song_path)
			return False
		else:
			logging.info("'%s' is adding song to queue: %s" % (user, song_path))
			self.queue.append({"user": user, "file": song_path, "title": self.filename_from_path(song_path)})
			self.update_queue()
			return True

	def queue_add_random(self, amount):
		logging.info("Adding %d random songs to queue" % amount)
		songs = list(self.available_songs)  # make a copy
		if len(songs) == 0:
			logging.warn("No available songs!")
			return False
		i = 0
		while i < amount:
			r = random.randint(0, len(songs) - 1)
			if self.is_song_in_queue(songs[r]):
				logging.warn("Song already in queue, trying another... " + songs[r])
			else:
				self.queue.append({"user": "Random", "file": songs[r], "title": self.filename_from_path(songs[r])})
				i += 1
			songs.pop(r)
			if len(songs) == 0:
				self.update_queue()
				logging.warn("Ran out of songs!")
				return False
		self.update_queue()
		return True

	def update_queue(self):
		self.queue_json = json.dumps(self.queue)
		self.status_dirty = True
		self.cancel_protected_best_jobs()

	def queued_protected_paths(self):
		return set([i['file'] for i in self.queue[:5]] + ([self.now_playing_filename] if self.now_playing_filename else []))

	def is_best_upgrade_candidate(self, song_path):
		if not self.accept_best_quality_work:
			return False
		if not self.best_quality_upgrader:
			return False
		if not os.path.isfile(song_path):
			return False
		if song_path in self.queued_protected_paths():
			return False
		youtube_id = self.get_youtube_id_from_path(song_path)
		if not youtube_id:
			return False
		if youtube_id in self.best_quality_active:
			return False
		meta = self.quality_metadata.get(youtube_id, {})
		if meta.get('best_available') and meta.get('quality_profile') == 'best':
			return False
		if not self.is_retry_ready(youtube_id):
			return False
		return True

	def classify_best_upgrade_candidate(self, song_path, protected_paths=None):
		if not self.accept_best_quality_work:
			return 'stopping'
		if not self.best_quality_upgrader:
			return 'disabled'
		if not os.path.isfile(song_path):
			return 'missing'
		protected_paths = protected_paths or self.queued_protected_paths()
		if song_path in protected_paths:
			return 'queue-protected'
		youtube_id = self.get_youtube_id_from_path(song_path)
		if not youtube_id:
			return 'no-youtube-id'
		if youtube_id in self.best_quality_active:
			return 'upgrading'
		meta = self.quality_metadata.get(youtube_id, {})
		if meta.get('best_available') and meta.get('quality_profile') == 'best':
			return 'already-best'
		if not self.is_retry_ready(youtube_id):
			return 'retry-backoff'
		return 'candidate'

	def get_best_upgrade_candidates(self):
		self.get_available_songs()
		protected_paths = self.queued_protected_paths()
		stats = Counter()
		candidates = []
		for song in list(self.available_songs):
			reason = self.classify_best_upgrade_candidate(song, protected_paths)
			stats[reason] += 1
			if reason == 'candidate':
				candidates.append(song)
		return candidates, stats

	def best_quality_worker(self):
		print("Best-quality background upgrader enabled", flush=True)
		while True:
			try:
				if not self.accept_best_quality_work:
					break
				candidates, stats = self.get_best_upgrade_candidates()
				if not candidates:
					self.flush_deferred_vocal_regeneration()
					print("Best-quality scan: no eligible songs (" + ", ".join([f"{k}={v}" for k, v in sorted(stats.items())]) + ")", flush=True)
					time.sleep(self.best_quality_scan_interval)
					continue
				print(f"Best-quality scan: upgrading 1 of {len(candidates)} eligible song(s): {os.path.basename(candidates[0])}", flush=True)
				self.upgrade_song_to_best(candidates[0])
			except Exception as e:
				logging.warning(f"Best-quality worker error: {e}")
				time.sleep(self.best_quality_scan_interval)

	def upgrade_song_to_best(self, song_path):
		youtube_id = self.get_youtube_id_from_path(song_path)
		if not youtube_id:
			return False
		if song_path in self.queued_protected_paths():
			return False
		self.best_quality_active.add(youtube_id)
		with self.quality_lock:
			meta = self.quality_metadata.get(youtube_id, {'youtube_id': youtube_id})
			meta['upgrade_state'] = 'upgrading'
			self.quality_metadata[youtube_id] = meta
			self.quality_metadata_dirty = True
			self.status_dirty = True
		self.save_quality_metadata()
		work_dir = None
		job = {
			'url': self.youtube_url_from_id(youtube_id),
			'old_path': song_path,
			'started_at': time.time(),
			'quality': 'best',
		}
		with self.download_lock:
			self.download_jobs[youtube_id] = job
		try:
			with self.best_quality_work_lock:
				best_tmp, sidecar, work_dir, _, quality_extra = self.download_best_quality_file(self.youtube_url_from_id(youtube_id), old_path=song_path, job=job)
				if song_path == self.now_playing_filename:
					raise RuntimeError('song became now playing before swap')
				if song_path in self.queued_protected_paths() and self.should_cancel_best_job(job):
					raise RuntimeError('song became protected by queue before swap')
				final_path = self.replace_library_file_with_best(song_path, best_tmp, sidecar, quality_extra, restart_vocal_after=False)
			logging.info(f"Best-quality upgrade complete: {final_path}")
			self.clear_quality_failure(youtube_id)
			return True
		except Exception as e:
			self.set_quality_failure(youtube_id, e)
			return False
		finally:
			self.best_quality_active.discard(youtube_id)
			with self.download_lock:
				self.download_jobs.pop(youtube_id, None)
			if work_dir:
				shutil.rmtree(work_dir, ignore_errors=True)

	def queue_clear(self):
		logging.info("Clearing queue!")
		self.queue = []
		self.update_queue()
		self.skip()

	def queue_edit(self, song_file, action, **kwargs):
		if action == "move":
			try:
				src, tgt, size = [int(kwargs[n]) for n in ['src', 'tgt', 'size']]
				if size > len(self.queue):
					# new songs have started while dragging the list
					diff = size - len(self.queue)
					src -= diff
					tgt -= diff
				song = self.queue.pop(src)
				self.queue.insert(tgt, song)
			except:
				logging.error("Invalid move song request: " + str(kwargs))
				return False
		else:
			match = [(ii,each) for ii,each in enumerate(self.queue) if song_file in each["file"]]
			index, song = match[0] if match else (-1, None)
			if song == None:
				logging.error("Song not found in queue: " + song["file"])
				return False
			if action == "up":
				if index < 1:
					logging.warn("Song is up next, can't bump up in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song up in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index - 1, song)
			elif action == "down":
				if index == len(self.queue) - 1:
					logging.warn("Song is already last, can't bump down in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song down in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index + 1, song)
			elif action == "delete":
				logging.info("Deleting song from queue: " + song["file"])
				del self.queue[index]
			else:
				logging.error("Unrecognized direction: " + action)
				return False
		self.update_queue()
		return True

	def skip(self):
		if self.is_file_playing():
			logging.info("Skipping: " + self.now_playing)
			if self.use_vlc:
				self.vlcclient.stop()
			else:
				self.omxclient.stop()
			self.reset_now_playing()
			return True
		logging.warning("Tried to skip, but no file is playing!")
		return False

	def seek(self, seek_sec):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.seek(seek_sec)
			else:
				logging.warning("OMXplayer cannot seek track!")
			return True
		logging.warning("Tried to seek, but no file is playing!")
		return False

	def set_delays_dict(self, filename, key, val, dft_val=0):
		basename = os.path.basename(filename)
		delays = self.delays.get(basename, {})
		if val == dft_val:
			delays.pop(key, None)
		else:
			delays[key] = val
		if delays:
			self.delays[basename] = delays
		else:
			self.delays.pop(basename, {})
		self.delays_dirty = True

	def set_audio_delay(self, delay):
		if delay == '+':
			self.audio_delay += 0.1
		elif delay == '-':
			self.audio_delay -= 0.1
		elif delay == '':
			self.audio_delay = 0
		else:
			try:
				self.audio_delay = float(delay)
			except:
				logging.warning(f"Tried to set audio delay to an invalid value {delay}, ignored!")
				return False

		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'audio_delay', self.audio_delay)

		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.command(f"audiodelay&val={self.audio_delay}")
			else:
				logging.warning("OMXplayer cannot set audio delay!")
			self.status_dirty = True
			return self.audio_delay
		logging.warning("Tried to set audio delay, but no file is playing!")
		return False

	def set_subtitle_delay(self, delay):
		if delay == '+':
			self.subtitle_delay += 0.1
		elif delay == '-':
			self.subtitle_delay -= 0.1
		elif delay == '':
			self.subtitle_delay = 0
		else:
			try:
				self.subtitle_delay = float(delay)
			except:
				logging.warning(f"Tried to set subtitle delay to an invalid value {delay}, ignored!")
				return False

		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'subtitle_delay', self.subtitle_delay)

		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.command(f"subdelay&val={self.subtitle_delay}")
			else:
				logging.warning("OMXplayer cannot set subtitle delay!")
			self.status_dirty = True
			return self.subtitle_delay
		logging.warning("Tried to set subtitle delay, but no file is playing!")
		return False

	def toggle_subtitle(self):
		self.show_subtitle = not self.show_subtitle
		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'show_subtitle', self.show_subtitle, True)
		self.play_vocal(force=True)

	def pause(self):
		if self.is_file_playing():
			logging.info("Toggling pause: " + self.now_playing)
			if self.use_vlc:
				if self.vlcclient.is_playing():
					self.vlcclient.pause()
					self.is_paused = True
				else:
					self.vlcclient.play()
					self.is_paused = False
			else:
				if self.omxclient.is_playing():
					self.omxclient.pause()
					self.is_paused = True
				else:
					self.omxclient.play()
					self.is_paused = False
			self.status_dirty = True
			return True
		else:
			logging.warning("Tried to pause, but no file is playing!")
			return False

	def vol_up(self):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.vol_up()
				xml = self.vlcclient.command().text
				self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			else:
				self.volume = self.omxclient.vol_up()
			self.update_logical_vol()
			return self.volume
		else:
			logging.warning("Tried to volume up, but no file is playing!")
			return False

	def vol_down(self):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.vol_down()
				xml = self.vlcclient.command().text
				self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			else:
				self.volume = self.omxclient.vol_down()
			self.update_logical_vol()
			return self.volume
		else:
			logging.warning("Tried to volume down, but no file is playing!")
			return False

	def vol_set(self, volume):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.vol_set(volume)
				xml = self.vlcclient.command().text
				self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			else:
				logging.warning("Only VLC player can set volume, ignored!")
				self.volume = self.omxclient.volume_offset
			self.update_logical_vol()
			return self.volume
		else:
			logging.warning("Tried to set volume, but no file is playing!")
			return False

	def play_speed_set(self, speed):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.playspeed_set(speed)
				xml = self.vlcclient.command().text
				self.play_speed = float(self.vlcclient.get_val_xml(xml, 'rate'))
				logging.info(f"Playback speed set to {self.play_speed}")
			else:
				logging.warning("Only VLC player can set playback speed, ignored!")
			return self.play_speed
		else:
			logging.warning("Tried to set play speed, but no file is playing!")
			return False

	def try_set_vocal_mode(self, mode, now_playing_filename):
		if mode not in ['mixed', 'vocal', 'nonvocal']:
			mode = {1: 'nonvocal', 2: 'mixed', 3: 'vocal'}[self.get_vocal_mode()]
		play_slave = '' if mode == 'mixed' else self.download_path + mode + '/' + ('' if self.use_DNN_vocal else '.') \
		                                       + os.path.basename(now_playing_filename) + '.m4a'
		if os.path.isfile(play_slave):
			self.vocal_mode = mode
		else:
			play_slave = ''
			self.vocal_mode = 'mixed'
		return play_slave

	def play_vocal(self, mode = None, force = False):
		# mode=vocal/nonvocal/mixed, or else (use current)
		if self.use_vlc:
			play_slave = self.try_set_vocal_mode(mode, self.now_playing_filename)
			if not force and self.now_playing_slave == play_slave:
				return
			status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
			info = self.vlcclient.get_info_xml(status_xml)
			posi = info['position']*info['length']
			self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))
			self.get_vocal_info(True)
		else:
			logging.error("Not using VLC. Can't play vocal/nonvocal.")

	def get_vocal_mode(self):
		if '/nonvocal/' in self.now_playing_slave.replace('\\', '/'):
			return 1
		elif '/vocal/' in self.now_playing_slave.replace('\\', '/'):
			return 3
		return 2

	def get_vocal_info(self, force_update=False):
		tm = time.time()
		if not force_update and tm-self.last_vocal_time < 2:
			return self.last_vocal_info
		if not self.now_playing_filename:
			return 0
		mask = 0
		bn = os.path.basename(self.now_playing_filename)
		if os.path.isfile(f'{self.download_path}nonvocal/{bn}.m4a'):
			mask |= 0b00000001
		if os.path.isfile(f'{self.download_path}vocal/{bn}.m4a'):
			mask |= 0b00000010
		if os.path.isfile(f'{self.download_path}nonvocal/.{bn}.m4a'):
			mask |= 0b00000100
		if os.path.isfile(f'{self.download_path}vocal/.{bn}.m4a'):
			mask |= 0b00001000
		if 'vocal/.' in self.now_playing_slave:
			mask |= 0b10000000
		if self.use_DNN_vocal:
			mask |= 0b01000000
		mask |= (self.get_vocal_mode() << 4)
		self.last_vocal_info = mask
		self.last_vocal_time = tm
		return mask

	def get_state(self):
		if self.use_vlc and self.vlcclient.is_transposing:
			return defaultdict(lambda: None, self.player_state)
		if not self.is_file_playing():
			self.player_state['now_playing'] = None
			return defaultdict(lambda: None, self.player_state)
		new_state = self.vlcclient.get_info_xml() if self.use_vlc else {
			'volume': self.omxclient.volume_offset,
			'state': ('paused' if self.omxclient.paused else 'playing')
		}
		self.player_state.update(new_state)
		return defaultdict(lambda: None, self.player_state)

	def restart(self):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.restart()
			else:
				self.omxclient.restart()
			self.is_paused = False
			return True
		else:
			logging.warning("Tried to restart, but no file is playing!")
			return False

	def stop(self):
		self.running = False

	def handle_run_loop(self):
		for event in pygame.event.get():
			if event.type == pygame.QUIT:
				logging.warn("Window closed: Exiting pikaraoke...")
				self.running = False
			elif event.type == pygame.KEYDOWN:
				if event.key == pygame.K_ESCAPE:
					logging.warn("ESC pressed: Exiting pikaraoke...")
					self.running = False
				if event.key == pygame.K_f:
					self.toggle_full_screen()
		if not self.is_file_playing() or not self.has_video:
			self.render_splash_screen()
			pygame.display.update()
		pygame.time.wait(100)

	# Use this to reset the screen in case it loses focus
	# This seems to occur in windows after playing a video
	def pygame_reset_screen(self):
		if not self.hide_splash_screen:
			logging.debug("Resetting pygame screen...")
			pygame.display.quit()
			self.initialize_screen()
			self.render_splash_screen()

	def reset_now_playing(self):
		self.auto_save_delays()
		self.now_playing = None
		self.now_playing_filename = None
		self.now_playing_user = None
		self.is_paused = True
		self.now_playing_transpose = 0
		self.now_playing_slave = ''
		self.audio_delay = 0
		self.subtitle_delay = self.default_subtitle_delay
		self.show_subtitle = True
		self.has_subtitle = False
		self.has_video = True
		self.last_vocal_info = 0
		self.play_speed = 1

	def streamer_alive(self):
		try:
			return bool([1 for p in psutil.process_iter() if './screencapture.sh' in p.cmdline()])
		except:
			return None

	def streamer_restart(self, delay=0):
		if self.platform in ['windows', 'osx']:
			return
		os.system(f"sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c && tmux send-keys -t PiKaraoke:0.3 Up Enter")

	def streamer_stop(self, delay=0):
		if self.platform in ['windows', 'osx']:
			return
		os.system(f"sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c")

	def vocal_alive(self):
		try:
			return bool(self.vocal_process and self.vocal_process.is_alive())\
					or bool([1 for p in psutil.process_iter() if 'vocal_splitter.py' in p.cmdline()])
		except:
			return None

	def vocal_restart(self):
		if self.platform == 'windows' or self.run_vocal:
			import vocal_splitter
			if self.vocal_process is not None and self.vocal_process.is_alive():
				self.vocal_process.kill()
			if shutil.which('ffmpeg'):
				self.vocal_process = mp.Process(target=vocal_splitter.main, args=(['-p', '-d', self.download_path],))
				self.vocal_process.start()
		else:
			os.system(f"tmux send-keys -t PiKaraoke:0.4 C-c && tmux send-keys -t PiKaraoke:0.4 Up Enter")

	def vocal_stop(self):
		if self.vocal_process is not None and self.vocal_process.is_alive():
			self.vocal_process.kill()
		elif self.platform != 'windows':
			os.system(f"tmux send-keys -t PiKaraoke:0.4 C-c")

	def drain_download_jobs_before_shutdown(self):
		self.accept_best_quality_work = False
		while True:
			with self.download_lock:
				active_jobs = list(self.download_jobs.values())
			active_processes = [
				job for job in active_jobs
				if job.get('process') is not None and job['process'].poll() is None
			]
			with self.quality_lock:
				active_best = self.active_best_quality_jobs
			if not active_jobs and active_best == 0:
				break
			logging.info(f"Waiting for {max(len(active_jobs), len(active_processes), active_best)} active download/conversion/verification job(s) before shutdown")
			time.sleep(5)

	def get_mp3_volume(self, filename):
		try:
			basename, md5, fsize = os.path.basename(filename), md5sum(filename), os.stat(filename).st_size
			vol_fsize_md5 = self.song2vol.get(basename, [0]*3)
			if fsize == vol_fsize_md5[1] and md5 == vol_fsize_md5[2]:
				return vol_fsize_md5[0]
			pcm_data = subprocess.check_output(['ffmpeg', '-i', filename, '-vn', '-f', 's16le', '-acodec', 'pcm_s16le', '-'], stderr = subprocess.DEVNULL)
			volume_val = np.clip(np.std(np.frombuffer(pcm_data, dtype = np.int16))/STD_VOL, 1/16, 16)
			self.song2vol[basename] = [volume_val, fsize, md5]
			with Open(self.download_path+'/.mp3_volume.json.gz', 'wt') as fp:
				json.dump(self.song2vol, fp, indent=1)
			return volume_val
		except:
			logging.warning(f"Could not analyse volume for {filename}, skipping normalisation for this song")
			return 1

	def update_logical_vol(self):
		if hasattr(self, 'media_vol'):
			self.logical_volume = self.volume * self.media_vol

	def enable_vol_norm(self, enable):
		self.normalize_vol = enable
		if enable and shutil.which('ffmpeg') is None:
			self.normalize_vol = enable = False
		if enable and self.now_playing_filename:
			self.volume = self.vlcclient.get_info_xml()['volume']
			self.media_vol = self.get_mp3_volume(self.now_playing_filename)
			self.update_logical_vol()
		self.save_config()
		return str(self.logical_volume)

	def set_dnn_vocal(self, enabled):
		self.use_DNN_vocal = enabled
		self.save_config()
		self.play_vocal()

	# Config file — plain INI format, safe to hand-edit while the app is not running.
	# Lives in the project folder (alongside app.py) rather than the song folder.
	CONFIG_TEMPLATE = """\
# pikaraoke settings
# This file is written automatically by the web UI.
# You can also edit it by hand while the app is not running.

[pikaraoke]

# Normalize volume levels across songs so loud and quiet songs play at similar levels.
# Requires ffmpeg to be installed.
normalize_vol = {normalize_vol}

# Use the DNN (neural network) model for vocal separation.
# Produces better quality results and uses the GPU if available.
# Set to false to use the faster stereo subtraction method instead.
use_dnn_vocal = {use_dnn_vocal}

# Save per-song play settings (audio delay, subtitle delay, subtitle on/off).
# Settings are stored alongside the song library.
save_play_settings = {save_play_settings}
"""

	def load_config(self):
		self.config_path = os.path.join(self.base_path, 'pikaraoke.cfg')
		if not os.path.isfile(self.config_path):
			logging.info(f"No config file found, creating defaults at {self.config_path}")
			self.save_config()
			return
		config = configparser.ConfigParser()
		config.read(self.config_path)
		if 'pikaraoke' in config:
			s = config['pikaraoke']
			self.normalize_vol = s.getboolean('normalize_vol', fallback=self.normalize_vol)
			self.use_DNN_vocal = s.getboolean('use_dnn_vocal', fallback=self.use_DNN_vocal)
			save_play_settings = s.getboolean('save_play_settings', fallback=bool(self.save_delays))
			self.set_save_delays(save_play_settings)
		logging.info(f"Config loaded from {self.config_path}")

	def save_config(self):
		try:
			with open(self.config_path, 'w') as f:
				f.write(self.CONFIG_TEMPLATE.format(
					normalize_vol=str(self.normalize_vol).lower(),
					use_dnn_vocal=str(self.use_DNN_vocal).lower(),
					save_play_settings=str(bool(self.save_delays)).lower(),
				))
			logging.info(f"Config saved to {self.config_path}")
		except Exception as e:
			logging.error(f"Failed to save config: {e}")

	def init_save_delays(self):
		self.delays_dirty = False
		if os.path.isfile(self.save_delays):
			try:
				self.delays = json.load(open(self.save_delays))
				return
			except:
				logging.warning(f"Could not read delays file {self.save_delays}, starting with empty delays")
		self.delays = {}
		with open(self.save_delays, 'w') as fp:
			json.dump(self.delays, fp, indent=1)

	def set_save_delays(self, state):
		if state != bool(self.save_delays):
			if state:
				self.save_delays = self.dft_delays_file
				self.init_save_delays()
			else:
				self.save_delays = None
		self.save_config()

	def auto_save_delays(self):
		if self.save_delays and self.delays_dirty:
			self.delays_dirty = False
			with open(self.save_delays, 'w') as fp:
				json.dump(self.delays, fp, indent=1)

	def run(self):
		logging.info("Starting PiKaraoke!")
		self.running = True
		if self.best_quality_upgrader:
			threading.Thread(target=self.best_quality_worker, daemon=True).start()

		# Windows does not have tmux, vocal splitter can only be invoked from the main program
		if self.platform == 'windows' or self.run_vocal:
			Try(lambda: self.vocal_restart())

		while self.running:
			try:
				if not self.is_file_playing() and self.now_playing != None:
					self.reset_now_playing()
				if self.queue:
					if not self.is_file_playing():
						self.reset_now_playing()
						self.render_splash_screen()
						tm = time.time()
						while time.time()-tm < self.splash_delay:
							self.handle_run_loop()
						head = self.queue.pop(0)
						self.play_file(head['file'])
						if not self.firstSongStarted:
							if self.streamer_alive():
								self.streamer_restart(1)
							self.firstSongStarted = True
						self.now_playing_user = head["user"]
						self.update_queue()
				self.handle_run_loop()
			except KeyboardInterrupt:
				logging.warn("Keyboard interrupt: Exiting pikaraoke...")
				self.running = False

		# Clean up before quit
		self.drain_download_jobs_before_shutdown()
		self.streamer_stop()
		self.vocal_stop()
		vplayer = self.vlcclient if self.use_vlc else self.omxclient
		if vplayer is not None: vplayer.stop()
		self.auto_save_delays()
		time.sleep(1)
		if vplayer is not None: vplayer.kill()
