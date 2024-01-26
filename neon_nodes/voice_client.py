# NEON AI (TM) SOFTWARE, Software Development Kit & Application Development System
# All trademark and other rights reserved by their respective owners
# Copyright 2008-2021 Neongecko.com Inc.
# BSD-3
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS  BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS;  OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE,  EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import io

from os.path import join, isfile, dirname
from threading import Thread
from unittest.mock import Mock
from base64 import b64decode, b64encode
from ovos_plugin_manager.microphone import OVOSMicrophoneFactory
from ovos_plugin_manager.vad import OVOSVADFactory
from ovos_dinkum_listener.voice_loop.voice_loop import DinkumVoiceLoop
from ovos_dinkum_listener.voice_loop.hotwords import HotwordContainer
from ovos_config.config import Configuration
from ovos_utils.messagebus import FakeBus
from ovos_utils import wait_for_exit_signal
from ovos_utils.log import LOG
from ovos_bus_client.message import Message
from neon_utils.hana_utils import request_backend, ServerException
from neon_utils.net_utils import get_adapter_info
from neon_utils.user_utils import get_default_user_config
from speech_recognition import AudioData
from pydub import AudioSegment
from pydub.playback import play


class MockTransformers(Mock):
    def transform(self, chunk):
        return chunk, dict()


def on_ready():
    LOG.info("ready")


def on_stopping():
    LOG.info("stopping")


def on_error(e="unknown"):
    LOG.error(e)


def on_alive():
    LOG.debug("alive")


def on_started():
    LOG.debug("started")


class NeonVoiceClient:
    def __init__(self, bus=None, ready_hook=on_ready, error_hook=on_error,
                 stopping_hook=on_stopping, alive_hook=on_alive,
                 started_hook=on_started):
        self.error_hook = error_hook
        self.stopping_hook = stopping_hook
        alive_hook()
        self.config = Configuration()
        LOG.init(self.config.get("logging"))
        self.bus = bus or FakeBus()
        self.lang = self.config.get('lang') or "en-us"
        self._mic = OVOSMicrophoneFactory.create(self.config)
        self._mic.start()
        self._hotwords = HotwordContainer(self.bus)
        self._hotwords.load_hotword_engines()
        self._vad = OVOSVADFactory.create(self.config)

        self._voice_loop = DinkumVoiceLoop(mic=self._mic,
                                           hotwords=self._hotwords,
                                           stt=Mock(),
                                           fallback_stt=Mock(),
                                           vad=self._vad,
                                           transformers=MockTransformers(),
                                           stt_audio_callback=self.on_stt_audio,
                                           listenword_audio_callback=
                                           self.on_hotword_audio)
        self._voice_loop.start()
        self._voice_thread = None

        self._listening_sound = None
        self._error_sound = None

        self._network_info = dict()
        self._node_data = dict()

        started_hook()
        self.run()
        ready_hook()

    @property
    def listening_sound(self) -> AudioSegment:
        if not self._listening_sound:
            res_file = Configuration().get('sounds').get('start_listening')
            if not isfile(res_file):
                res_file = join(dirname(__file__), "res", "start_listening.wav")
            self._listening_sound = AudioSegment.from_file(res_file,
                                                           format="wav")
        return self._listening_sound

    @property
    def error_sound(self) -> AudioSegment:
        if not self._error_sound:
            res_file = Configuration().get('sounds').get('error')
            if not isfile(res_file):
                res_file = join(dirname(__file__), "res", "error.wav")
            self._error_sound = AudioSegment.from_file(res_file, format="wav")
        return self._error_sound

    @property
    def network_info(self) -> dict:
        if not self._network_info:
            self._network_info = get_adapter_info()
        return self._network_info

    @property
    def node_data(self):
        if not self._node_data:
            self._node_data = {"device_description": "test client",
                               "networking": {
                                   "local_ip": self.network_info.get('ipv4'),
                                   "mac_address": self.network_info.get('mac')}
                               }
        return self._node_data

    @property
    def user_profile(self):
        return get_default_user_config()

    def run(self):
        try:
            self._voice_thread = Thread(target=self._voice_loop.run,
                                        daemon=True)
            self._voice_thread.start()
        except Exception as e:
            self.error_hook(repr(e))
            raise e

    def on_stt_audio(self, audio_bytes: bytes, context: dict):
        LOG.debug(f"Got {len(audio_bytes)} bytes of audio")
        wav_data = AudioData(audio_bytes, self._mic.sample_rate,
                             self._mic.sample_width).get_wav_data()
        try:
            self.get_audio_response(wav_data)
        except ServerException as e:
            LOG.error(e)
            play(self.error_sound)

    def on_hotword_audio(self, audio: bytes, context: dict):
        payload = context
        msg_type = "recognizer_loop:wakeword"
        play(self.listening_sound)
        LOG.info(f"Emitting hotword event: {msg_type}")
        # emit ww event
        self.bus.emit(Message(msg_type, payload, context))

    def get_audio_response(self, audio: bytes):
        audio_data = b64encode(audio).decode("utf-8")
        transcript = request_backend("neon/get_stt",
                                     {"encoded_audio": audio_data,
                                      "lang_code": self.lang})
        transcribed = transcript['transcripts'][0]
        LOG.info(transcribed)
        response = request_backend("neon/get_response",
                                   {"lang_code": self.lang,
                                    "user_profile": self.user_profile,
                                    "node_data": self.node_data,
                                    "utterance": transcribed})
        answer = response['answer']
        LOG.info(answer)
        audio = request_backend("neon/get_tts", {"lang_code": self.lang,
                                                 "to_speak": answer})
        audio_bytes = b64decode(audio['encoded_audio'])
        play(AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav"))
        LOG.info(f"Playback completed")

    def shutdown(self):
        self.stopping_hook()
        self._voice_loop.stop()
        self._voice_thread.join(30)


def main(*args, **kwargs):
    client = NeonVoiceClient(*args, **kwargs)
    wait_for_exit_signal()
    client.shutdown()


if __name__ == "__main__":
    # environ.setdefault("OVOS_CONFIG_BASE_FOLDER", "neon")
    # environ.setdefault("OVOS_CONFIG_FILENAME", "diana.yaml")
    main()
