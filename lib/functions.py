import argparse
import asyncio
import csv
import docker
import ebooklib
import fnmatch
import gc
import gradio as gr
import hashlib
import json
import numpy as np
import os
import psutil
import random
import regex as re
import requests
import shutil
import socket
import subprocess
import sys
import time
import torch
import torchaudio
import urllib.request
import uuid
import zipfile
import traceback

from bs4 import BeautifulSoup
from collections import Counter
from collections.abc import Mapping
from collections.abc import MutableMapping
from datetime import datetime
from ebooklib import epub
from functools import lru_cache
from glob import glob
from huggingface_hub import hf_hub_download
from iso639 import languages
from multiprocessing import Manager, Event
from multiprocessing.managers import DictProxy, ListProxy
from pathlib import Path
from pydub import AudioSegment
from threading import Lock
from tqdm import tqdm
from translate import Translator
from TTS.api import TTS as XTTS
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts
from types import MappingProxyType
from urllib.parse import urlparse

import lib.conf as conf
import lib.lang as lang
import lib.models as mod

def inject_configs(target_namespace):
    # Extract variables from both modules and inject them into the target namespace
    for module in (conf, lang, mod):
        target_namespace.update({k: v for k, v in vars(module).items() if not k.startswith('__')})

# Inject configurations into the global namespace of this module
inject_configs(globals())

class DependencyError(Exception):
    def __init__(self, message=None):
        super().__init__(message)
        # Automatically handle the exception when it's raised
        self.handle_exception()

    def handle_exception(self):
        # Print the full traceback of the exception
        traceback.print_exc()
        
        # Print the exception message
        print(f'Caught DependencyError: {self}')
        
        # Exit the script if it's not a web process
        if not is_gui_process:
            sys.exit(1)

def recursive_proxy(data, manager=None):
    """Recursively convert a nested dictionary into Manager.dict proxies."""
    if manager is None:
        manager = Manager()
    if isinstance(data, dict):
        proxy_dict = manager.dict()
        for key, value in data.items():
            proxy_dict[key] = recursive_proxy(value, manager)
        return proxy_dict
    elif isinstance(data, list):
        proxy_list = manager.list()
        for item in data:
            proxy_list.append(recursive_proxy(item, manager))
        return proxy_list
    elif isinstance(data, (str, int, float, bool, type(None))):
        return data
    else:
        raise TypeError(f"Unsupported data type: {type(data)}")

class ConversionContext:
    def __init__(self):
        self.manager = Manager()
        self.sessions = self.manager.dict()  # Store all session-specific contexts
        self.cancellation_events = {}  # Store multiprocessing.Event for each session

    def get_session(self, id):
        """Retrieve or initialize session-specific context"""
        if id not in self.sessions:
            self.sessions[id] = recursive_proxy({
                "script_mode": NATIVE,
                "id": id,
                "process_id": None,
                "device": default_device,
                "client": None,
                "language": default_language_code,
                "language_iso1": None,
                "audiobook": None,
                "audiobooks_dir": None,
                "process_dir": None,
                "src": None,
                "chapters_dir": None,
                "chapters_dir_sentences": None,
                "epub_path": None,
                "filename_noext": None,
                "tts_engine": default_tts_engine,
                "fine_tuned": default_fine_tuned,
                "voice": None,
                "voice_dir": None,
                "custom_model": None,
                "custom_model_dir": None,
                "chapters": None,
                "cover": None,
                "status": None,
                "progress": 0,
                "time": None,
                "cancellation_requested": False,
                "temperature": tts_default_settings['temperature'],
                "length_penalty": tts_default_settings['length_penalty'],
                "repetition_penalty": tts_default_settings['repetition_penalty'],
                "top_k": tts_default_settings['top_k'],
                "top_p": tts_default_settings['top_k'],
                "speed": tts_default_settings['speed'],
                "enable_text_splitting": tts_default_settings['enable_text_splitting'],
                "event": None,
                "metadata": {
                    "title": None, 
                    "creator": None,
                    "contributor": None,
                    "language": None,
                    "identifier": None,
                    "publisher": None,
                    "date": None,
                    "description": None,
                    "subject": None,
                    "rights": None,
                    "format": None,
                    "type": None,
                    "coverage": None,
                    "relation": None,
                    "Source": None,
                    "Modified": None,
                }
            }, manager=self.manager)
        return self.sessions[id]
        
context = ConversionContext()
lock = Lock()
is_gui_process = False


def prepare_dirs(src, session):
    try:
        resume = False
        os.makedirs(os.path.join(models_dir,'tts'), exist_ok=True)
        os.makedirs(session['session_dir'], exist_ok=True)
        os.makedirs(session['process_dir'], exist_ok=True)
        os.makedirs(session['custom_model_dir'], exist_ok=True)
        os.makedirs(session['voice_dir'], exist_ok=True)
        os.makedirs(session['audiobooks_dir'], exist_ok=True)
        session['src'] = os.path.join(session['process_dir'], os.path.basename(src))
        if os.path.exists(session['src']):
            if compare_files_by_hash(session['src'], src):
                resume = True
        if not resume:
            shutil.rmtree(session['chapters_dir'], ignore_errors=True)
        os.makedirs(session['chapters_dir'], exist_ok=True)
        os.makedirs(session['chapters_dir_sentences'], exist_ok=True)
        shutil.copy(src, session['src']) 
        return True
    except Exception as e:
        raise DependencyError(e)

def check_programs(prog_name, command, options):
    try:
        subprocess.run([command, options], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True, None
    except FileNotFoundError:
        e = f'''********** Error: {prog_name} is not installed! if your OS calibre package version 
        is not compatible you still can run ebook2audiobook.sh (linux/mac) or ebook2audiobook.cmd (windows) **********'''
        raise DependencyError(e)
    except subprocess.CalledProcessError:
        e = f'Error: There was an issue running {prog_name}.'
        raise DependencyError(e)

import os
import zipfile

def analyze_uploaded_file(zip_path, required_files=None):
    try:
        if not os.path.exists(zip_path):
            raise FileNotFoundError(f"The file does not exist: {os.path.basename(zip_path)}")
        if required_files is None:
            required_files = default_xtts_files
        executable_extensions = {'.exe', '.bat', '.cmd', '.bash', '.bin', '.sh', '.msi', '.dll', '.com'}
        files_in_zip = {}
        executables_found = False
        empty_files = set()
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for file_info in zf.infolist():
                file_name = file_info.filename
                if file_info.is_dir():
                    continue  # Skip directories
                base_name = os.path.basename(file_name)
                files_in_zip[base_name.lower()] = file_info.file_size
                _, ext = os.path.splitext(base_name.lower())
                if ext in executable_extensions:
                    executables_found = True
                    break
                if file_info.file_size == 0:
                    empty_files.add(base_name.lower())
        required_files = [file.lower() for file in required_files]
        missing_files = [f for f in required_files if f not in files_in_zip]
        required_empty_files = [f for f in required_files if f in empty_files]
        if executables_found:
            print("Executables found in the ZIP file.")
        if missing_files:
            print(f"Missing required files: {missing_files}")
        if required_empty_files:
            print(f"Required files with 0 KB: {required_empty_files}")
        return not executables_found and not missing_files and not required_empty_files
    except zipfile.BadZipFile:
        raise ValueError("The file is not a valid ZIP archive.")
    except Exception as e:
        raise RuntimeError(f"An error occurred: {e}")

def extract_custom_model(file_src, session, required_files=None):
    try:
        if required_files is None:
            required_files = default_xtts_files
        model_dir = session['custom_model_dir']
        dir_src = os.path.dirname(file_src)
        checkpoint = os.path.basename(file_src).replace('.zip', '')
        with zipfile.ZipFile(file_src, 'r') as zip_ref:
            files = zip_ref.namelist()
            files_length = len(files)
            dir_tts = 'fairseq'
            xtts_config = 'config.json'
            config_data = {}
            if xtts_config in zip_ref.namelist():
                with zip_ref.open(xtts_config) as f:
                    config_data = json.load(f)
            if config_data.get('model') == 'xtts':
                dir_tts = 'xtts'          
            checkpoint_path = os.path.join(model_dir, dir_tts, checkpoint)
            os.makedirs(checkpoint_path, exist_ok=True)
            with tqdm(total=files_length, unit="files") as t:
                for f in files:
                    if f in required_files:
                        zip_ref.extract(f, checkpoint_path)
                    t.update(1)
        os.remove(file_src)
        print(f'Extracted files to {checkpoint_path}')
        return checkpoint_path
    except asyncio.exceptions.CancelledError:
        raise DependencyError(e)
        os.remove(file_src)
        return None       
    except Exception as e:
        raise DependencyError(e)
        os.remove(file_src)
        return None
        
def hash_proxy_dict(proxy_dict):
    return hashlib.md5(str(proxy_dict).encode('utf-8')).hexdigest()

def calculate_hash(filepath, hash_algorithm='sha256'):
    hash_func = hashlib.new(hash_algorithm)
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):  # Read in chunks to handle large files
            hash_func.update(chunk)
    return hash_func.hexdigest()

def compare_files_by_hash(file1, file2, hash_algorithm='sha256'):
    return calculate_hash(file1, hash_algorithm) == calculate_hash(file2, hash_algorithm)

def compare_dict_keys(d1, d2):
    if not isinstance(d1, Mapping) or not isinstance(d2, Mapping):
        return d1 == d2
    d1_keys = set(d1.keys())
    d2_keys = set(d2.keys())
    missing_in_d2 = d1_keys - d2_keys
    missing_in_d1 = d2_keys - d1_keys
    if missing_in_d2 or missing_in_d1:
        return {
            "missing_in_d2": missing_in_d2,
            "missing_in_d1": missing_in_d1,
        }
    for key in d1_keys.intersection(d2_keys):
        nested_result = compare_keys(d1[key], d2[key])
        if nested_result:
            return {key: nested_result}
    return None

def proxy_to_dict(proxy_obj):
    def recursive_copy(source, visited):
        # Handle circular references by tracking visited objects
        if id(source) in visited:
            return None  # Stop processing circular references
        visited.add(id(source))  # Mark as visited
        if isinstance(source, dict):
            result = {}
            for key, value in source.items():
                result[key] = recursive_copy(value, visited)
            return result
        elif isinstance(source, list):
            return [recursive_copy(item, visited) for item in source]
        elif isinstance(source, set):
            return list(source)
        elif isinstance(source, (int, float, str, bool, type(None))):
            return source
        elif isinstance(source, DictProxy):
            # Explicitly handle DictProxy objects
            return recursive_copy(dict(source), visited)  # Convert DictProxy to dict
        else:
            return str(source)  # Convert non-serializable types to strings
    return recursive_copy(proxy_obj, set())

'''
def has_metadata(f):
    try:
        b = epub.read_epub(f)
        metadata = b.get_metadata('DC', '')
        if metadata:
            return True
        else:
            return False
    except Exception as e:
        return False
'''

def convert_to_epub(session):
    if session['cancellation_requested']:
        #stop_and_detach_tts()
        print('Cancel requested')
        return False
    if session['script_mode'] == DOCKER_UTILS:
        try:
            docker_dir = os.path.basename(session['process_dir'])
            docker_file_in = os.path.basename(session['src'])
            docker_file_out = os.path.basename(session['epub_path'])
            
            # Check if the input file is already an EPUB
            if docker_file_in.lower().endswith('.epub'):
                shutil.copy(session['src'], session['epub_path'])
                return True

            # Convert the ebook to EPUB format using utils Docker image
            container = session['client'].containers.run(
                docker_utils_image,
                command=f'ebook-convert /files/{docker_dir}/{docker_file_in} /files/{docker_dir}/{docker_file_out}',
                volumes={session['process_dir']: {'bind': f'/files/{docker_dir}', 'mode': 'rw'}},
                remove=True,
                detach=False,
                stdout=True,
                stderr=True
            )
            print(container.decode('utf-8'))
            return True
        except docker.errors.ContainerError as e:
            raise DependencyError(e)
        except docker.errors.ImageNotFound as e:
            raise DependencyError(e)
        except docker.errors.APIError as e:
            raise DependencyError(e)
    else:
        try:
            util_app = shutil.which('ebook-convert')
            subprocess.run([util_app, session['src'], session['epub_path']], check=True)
            return True
        except subprocess.CalledProcessError as e:
            raise DependencyError(e)

def get_cover(epubBook, session):
    try:
        if session['cancellation_requested']:
            #stop_and_detach_tts()
            print('Cancel requested')
            return False
        cover_image = False
        cover_path = os.path.join(session['process_dir'], session['filename_noext'] + '.jpg')
        for item in epubBook.get_items_of_type(ebooklib.ITEM_COVER):
            cover_image = item.get_content()
            break
        if not cover_image:
            for item in epubBook.get_items_of_type(ebooklib.ITEM_IMAGE):
                if 'cover' in item.file_name.lower() or 'cover' in item.get_id().lower():
                    cover_image = item.get_content()
                    break
        if cover_image:
            with open(cover_path, 'wb') as cover_file:
                cover_file.write(cover_image)
                return cover_path
        return True
    except Exception as e:
        raise DependencyError(e)
'''
def get_chapters(epubBook, session):
    try:
        if session['cancellation_requested']:
            #stop_and_detach_tts()
            print('Cancel requested')
            return False
        all_docs = list(epubBook.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        if all_docs:
            all_docs = all_docs[1:]
            doc_patterns = [filter_pattern(str(doc)) for doc in all_docs if filter_pattern(str(doc))]
            most_common_pattern = filter_doc(doc_patterns)
            selected_docs = [doc for doc in all_docs if filter_pattern(str(doc)) == most_common_pattern]
            chapters = [filter_chapter(doc, session['language']) for doc in selected_docs]
            if session['metadata'].get('creator'):
                intro = f"{session['metadata']['creator']}\n\n{session['metadata']['title']}\n\n"
                chapters[0].insert(0, intro)
            return chapters
        return False
    except Exception as e:
        raise DependencyError(f'Error extracting main content pages: {e}')
'''

def get_chapters(epubBook, session):
    try:
        if session['cancellation_requested']:
            print('Cancel requested')
            return False      
        # Step 1: Get all documents and their patterns
        all_docs = list(epubBook.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        if not all_docs:
            return False
        all_docs = all_docs[1:]  # Exclude the first document if needed
        doc_patterns = [filter_pattern(str(doc)) for doc in all_docs if filter_pattern(str(doc))]        
        # Step 2: Determine the most common pattern
        most_common_pattern = filter_doc(doc_patterns)
        selected_docs = [doc for doc in all_docs if filter_pattern(str(doc)) == most_common_pattern]       
        # Step 3: Calculate average character length of selected docs
        char_lengths = [len(filter_chapter(doc, session['language'])) for doc in selected_docs]
        average_char_length = sum(char_lengths) / len(char_lengths) if char_lengths else 0
        # Step 4: Filter docs based on average character length or repetitive pattern
        final_selected_docs = []
        for doc in all_docs:
            doc_pattern = filter_pattern(str(doc))
            doc_content = filter_chapter(doc, session['language'])
            if len(doc_content) >= average_char_length or doc_pattern == most_common_pattern:
                final_selected_docs.append(doc)
        # Step 5: Extract chapters from the final selected docs
        chapters = [filter_chapter(doc, session['language']) for doc in final_selected_docs]
        if session['metadata'].get('creator'):
            intro = f"{session['metadata']['creator']}\n\n{session['metadata']['title']}\n\n"
            chapters[0].insert(0, intro)
        return chapters
    except Exception as e:
        raise DependencyError(f'Error extracting main content pages: {e}')

def filter_doc(doc_patterns):
    pattern_counter = Counter(doc_patterns)
    # Returns a list with one tuple: [(pattern, count)] 
    most_common = pattern_counter.most_common(1)
    return most_common[0][0] if most_common else None

def filter_pattern(doc_identifier):
    parts = doc_identifier.split(':')
    if len(parts) > 2:
        segment = parts[1]
        if re.search(r'[a-zA-Z]', segment) and re.search(r'\d', segment):
            return ''.join([char for char in segment if char.isalpha()])
        elif re.match(r'^[a-zA-Z]+$', segment):
            return segment
        elif re.match(r'^\d+$', segment):
            return 'numbers'
    return None

def filter_chapter(doc, language):
    soup = BeautifulSoup(doc.get_body_content(), 'html.parser')
    # Remove scripts and styles
    for script in soup(["script", "style"]):
        script.decompose()
    # Normalize lines and remove unnecessary spaces
    text = re.sub(r'(\r\n|\r|\n){3,}', '\r\n', soup.get_text().strip())
    text = replace_roman_numbers(text)
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = '\n'.join(chunk for chunk in chunks if chunk)
    text = text.replace('»', '"').replace('«', '"')
    # Pattern 1: Add a space between UTF-8 characters and numbers
    text = re.sub(r'(?<=[\p{L}])(?=\d)|(?<=\d)(?=[\p{L}])', ' ', text)
    # Pattern 2: Split numbers into groups of 4
    text = re.sub(r'(\d{4})(?=\d)', r'\1 ', text)
    chapter_sentences = get_sentences(text, language)
    return chapter_sentences

def get_sentences(sentence, language, max_pauses=5):
    max_length = language_mapping[language]['char_limit']
    punctuation = language_mapping[language]['punctuation']
    sentence = re.sub(r"\.(\s|$)", r" .\n", sentence)
    replacements = {
        '„': ' " ',  # German, Polish, Czech opening
        '“': ' " ',  # German, Polish, Czech, etc. closing
        '«': ' " ',  # French, Russian opening
        '»': ' " ',  # French, Russian closing
        '「': ' " ',  # Japanese opening
        '」': ' " ',  # Japanese closing
        '”': ' " ',   # Chinese, Korean closing
        '…': ' ... ',
        '–': ' - ',
        '—': ' - ',
        ':': ' : ',
        '\xa0': ' '  # Non-breaking space
    }
    for old, new in replacements.items():
        sentence = sentence.replace(old, new)
    parts = []
    while len(sentence) > max_length or sum(sentence.count(p) for p in punctuation) > max_pauses:
        # Step 1: Look for the last period (.) within max_length
        possible_splits = [i for i, char in enumerate(sentence[:max_length]) if char == ";\n"]
        # Step 2: If no periods, look for the last comma (,)
        if not possible_splits:
            possible_splits = [i for i, char in enumerate(sentence[:max_length]) if char == ',']    
        # Step 3: If still no splits, look for any other punctuation
        if not possible_splits:
            possible_splits = [i for i, char in enumerate(sentence[:max_length]) if char in punctuation]    
        # Step 4: Determine where to split the sentence
        if possible_splits:
            split_at = possible_splits[-1] + 1  # Split at the last occurrence of punctuation
        else:
            # If no punctuation is found, split at the last space
            last_space = sentence.rfind(' ', 0, max_length)
            if last_space != -1:
                split_at = last_space + 1
            else:
                # If no space is found, force split at max_length
                split_at = max_length   
        # Add the split sentence to parts
        parts.append(sentence[:split_at].strip() + ' ')
        sentence = sentence[split_at:].strip()
    # Add the remaining sentence if any
    if sentence:
        parts.append(sentence.strip() + ' ')
    return parts

def normalize_voice_file(f, session):
    final_name = os.path.splitext(os.path.basename(f))[0].replace('&', 'And').replace(' ', '_') + '.' + audioproc_format
    final_file = os.path.join(session['voice_dir'], final_name)    
    if session['script_mode'] == DOCKER_UTILS:
        docker_dir = os.path.basename(session['voice_dir'])
        ffmpeg_input_file = f'/files/{docker_dir}/' + os.path.basename(f)
        ffmpeg_final_file = f'/files/{docker_dir}/' + os.path.basename(final_file)                           
        ffmpeg_cmd = ['ffmpeg', '-i', ffmpeg_input_file]
    else:
        ffmpeg_input_file = f
        ffmpeg_final_file = final_file                  
        ffmpeg_cmd = [shutil.which('ffmpeg'), '-i', ffmpeg_input_file]
    print(ffmpeg_cmd)
    ffmpeg_cmd += [
        '-af', 'agate=threshold=-25dB:ratio=1.4:attack=10:release=250,'
               'afftdn=nf=-70,'
               'acompressor=threshold=-20dB:ratio=2:attack=80:release=200:makeup=1dB,'
               'loudnorm=I=-16:TP=-3:LRA=7:linear=true,'
               'equalizer=f=150:t=q:w=2:g=1,'
               'equalizer=f=250:t=q:w=2:g=-3,'
               'equalizer=f=3000:t=q:w=2:g=1,'
               'equalizer=f=5500:t=q:w=2:g=-4,'
               'equalizer=f=9000:t=q:w=2:g=-2,'
               'highpass=f=63',
        '-y', ffmpeg_final_file
    ]
    if session['script_mode'] == DOCKER_UTILS:
        try:
            container = session['client'].containers.run(
                docker_utils_image,
                command=ffmpeg_cmd,
                volumes={session['voice_dir']: {'bind': f'/files/{docker_dir}', 'mode': 'rw'}},
                remove=True,
                detach=False,
                stdout=True,
                stderr=True
            )
            print(container.decode('utf-8'))
            if shutil.copy(docker_final_file, final_file):
                return final_file
            error = f'Could not copy from {docker_final_file} to {final_file}'
        except docker.errors.ContainerError as e:
            error = f"normalize_voice_file() error: {e}"
            raise DependencyError(e)
        except docker.errors.ImageNotFound as e:
            error = f"normalize_voice_file() error: {e}"
            raise DependencyError(e)
        except docker.errors.APIError as e:
            error = f"normalize_voice_file() error: {e}"
            raise DependencyError(e)
        print(error)
        return None
    else:
        try:
            subprocess.run(ffmpeg_cmd, env={}, check=True)
            return final_file
        except subprocess.CalledProcessError as e:
            error = f"normalize_voice_file() error: {e}"
            raise DependencyError(e)
        except Exception as e:
            error = f"normalize_voice_file() error: {e}"
            raise DependencyError(e)
        print(error)
        return None

@lru_cache(maxsize=None)  # Cache based on unique arguments
def load_tts_api_cached(model_path):
    with lock:
        tts = XTTS(model_path)
    return tts
        
@lru_cache(maxsize=None)  # Cache based on unique arguments
def load_tts_custom_cached(model_path, config_path, vocab_path):
    config = XttsConfig()
    config.models_dir = os.path.join(models_dir,'tts')
    config.load_json(config_path)
    tts = Xtts.init_from_config(config)
    with lock:
        tts.load_checkpoint(config, checkpoint_path=model_path, vocab_path=vocab_path, eval=True)
    return tts

def init_cache(func, memory_threshold_mb=1024):
    available_memory_mb = psutil.virtual_memory().available // (1024 * 1024)
    if available_memory_mb < memory_threshold_mb:
        func.cache_clear()

def convert_chapters_to_audio(session):
    try:
        if session['cancellation_requested']:
            #stop_and_detach_tts()
            print('Cancel requested')
            return False
        progress_bar = None
        params = {}
        if is_gui_process:
            progress_bar = gr.Progress(track_tqdm=True)        
        params['tts_model'] = None
        '''
        # List available TTS base models
        print("Available Models:")
        print("=================")
        for index, model in enumerate(XTTS().list_models(), 1):
            print(f"{index}. {model}")
        '''
        if session['tts_engine'] == 'xtts':
            params['tts_model'] = 'xtts'
            if session['custom_model'] is not None:
                init_cache(load_tts_custom_cached)
                print(f"Loading TTS {params['tts_model']} model from {session['custom_model']}...")
                model_path = os.path.join(session['custom_model'], 'model.pth')
                config_path = os.path.join(session['custom_model'],'config.json')
                vocab_path = os.path.join(session['custom_model'],'vocab.json')
                voice_path = os.path.join(session['custom_model'],'ref.wav')
                params['tts'] = load_tts_custom_cached(model_path, config_path, vocab_path)
                print('Computing speaker latents...')
                params['voice_path'] = session['voice'] if session['voice'] is not None else voice_path
                params['gpt_cond_latent'], params['speaker_embedding'] = params['tts'].get_conditioning_latents(audio_path=[params['voice_path']])
            elif session['fine_tuned'] != 'std':
                init_cache(load_tts_custom_cached)
                print(f"Loading TTS {params['tts_model']} model from {session['fine_tuned']}...")
                hf_repo = models[params['tts_model']][session['fine_tuned']]['repo']
                hf_sub = models[params['tts_model']][session['fine_tuned']]['sub']
                cache_dir = os.path.join(models_dir,'tts')
                model_path = hf_hub_download(repo_id=hf_repo, filename=f"{hf_sub}/model.pth", cache_dir=cache_dir)
                config_path = hf_hub_download(repo_id=hf_repo, filename=f"{hf_sub}/config.json", cache_dir=cache_dir)
                vocab_path = hf_hub_download(repo_id=hf_repo, filename=f"{hf_sub}/vocab.json", cache_dir=cache_dir)    
                params['tts'] = load_tts_custom_cached(model_path, config_path, vocab_path)
                print('Computing speaker latents...')
                params['voice_path'] = session['voice'] if session['voice'] is not None else models[params['tts_model']][session['fine_tuned']]['voice']
                params['gpt_cond_latent'], params['speaker_embedding'] = params['tts'].get_conditioning_latents(audio_path=[params['voice_path']])
            else:
                init_cache(load_tts_api_cached)
                print(f"Loading TTS {params['tts_model']} model from {models[params['tts_model']][session['fine_tuned']]['repo']}...")
                model_path = models[params['tts_model']][session['fine_tuned']]['repo']
                params['tts'] = load_tts_api_cached(model_path)
                params['voice_path'] = session['voice'] if session['voice'] is not None else models[params['tts_model']][session['fine_tuned']]['voice']
            params['tts'].to(session['device'])
        elif session['tts_engine'] == 'fairseq':
            if session['custom_model'] is not None:
                print("TODO!")
            else:
                init_cache(load_tts_api_cached)
                params['tts_model'] = 'fairseq'
                model_path = models[params['tts_model']][session['fine_tuned']]['repo'].replace("[lang]", session['language'])
                print(f"Loading TTS {model_path} model from {model_path}...")
                params['tts'] = load_tts_api_cached(model_path)
                params['voice_path'] = session['voice'] if session['voice'] is not  None else models[params['tts_model']][session['fine_tuned']]['voice']
                params['tts'].to(session['device'])
        elif session['tts_engine'] == 'yourtts':
            print("TODO!")

        resume_chapter = 0
        resume_sentence = 0

        # Check existing files to resume the process if it was interrupted
        existing_chapters = sorted([f for f in os.listdir(session['chapters_dir']) if f.endswith(f'.{audioproc_format}')])
        existing_sentences = sorted([f for f in os.listdir(session['chapters_dir_sentences']) if f.endswith(f'.{audioproc_format}')])

        if existing_chapters:
            count_chapter_files = len(existing_chapters)
            resume_chapter = count_chapter_files - 1 if count_chapter_files > 0 else 0
            print(f'Resuming from chapter {count_chapter_files}')
        if existing_sentences:
            resume_sentence = len(existing_sentences)
            print(f'Resuming from sentence {resume_sentence}')

        total_chapters = len(session['chapters'])
        total_sentences = sum(len(array) for array in session['chapters'])
        current_sentence = 0

        with tqdm(total=total_sentences, desc='convert_chapters_to_audio 0.00%', bar_format='{desc}: {n_fmt}/{total_fmt} ', unit='step', initial=resume_sentence) as t:
            t.n = resume_sentence
            for x in range(resume_chapter, total_chapters):
                chapter_num = x + 1
                chapter_audio_file = f'chapter_{chapter_num}.{audioproc_format}'
                sentences = session['chapters'][x]
                sentences_count = len(sentences)
                start = current_sentence  # Mark the starting sentence of the chapter
                print(f"\nChapter {chapter_num} containing {sentences_count} sentences...")
                for i, sentence in enumerate(sentences):
                    if current_sentence >= resume_sentence:
                        params['sentence_audio_file'] = os.path.join(session['chapters_dir_sentences'], f'{current_sentence}.{audioproc_format}')                       
                        params['sentence'] = sentence
                        if convert_sentence_to_audio(params, session):                           
                            percentage = (current_sentence / total_sentences) * 100
                            t.set_description(f'Converting {percentage:.2f}%')
                            print(f'\nSentence: {sentence}')
                            t.update(1)
                            if progress_bar is not None:
                                progress_bar(current_sentence / total_sentences)
                        else:
                            return False
                    current_sentence += 1
                end = current_sentence - 1
                print(f"\nEnd of Chapter {chapter_num}")
                if start >= resume_sentence:
                    if combine_audio_sentences(chapter_audio_file, start, end, session):
                        print(f'Combining chapter {chapter_num} to audio, sentence {start} to {end}')
                    else:
                        print('combine_audio_sentences() failed!')
                        return False
        return True
    except Exception as e:
        raise DependencyError(e)

def convert_sentence_to_audio(params, session):
    try:
        if session['cancellation_requested']:
            #stop_and_detach_tts(params['tts'])
            print('Cancel requested')
            return False
        generation_params = {
            "temperature": session['temperature'],
            "length_penalty": session["length_penalty"],
            "repetition_penalty": session['repetition_penalty'],
            "num_beams": int(session['length_penalty']) + 1 if session["length_penalty"] > 1 else 1,
            "top_k": session['top_k'],
            "top_p": session['top_p'],
            "speed": session['speed'],
            "enable_text_splitting": session['enable_text_splitting']
        }
        if params['tts_model'] == 'xtts':
            if session['custom_model'] is not None or session['fine_tuned'] != 'std':
                with torch.no_grad():
                    output = params['tts'].inference(
                        text=params['sentence'],
                        language=session['language_iso1'],
                        gpt_cond_latent=params['gpt_cond_latent'],
                        speaker_embedding=params['speaker_embedding'],
                        **generation_params
                    )
                torchaudio.save(
                    params['sentence_audio_file'], 
                    torch.tensor(output[audioproc_format]).unsqueeze(0), 
                    sample_rate=24000
                )
                del output
            else:
                with torch.no_grad():
                    params['tts'].tts_to_file(
                        text=params['sentence'],
                        language=session['language_iso1'],
                        file_path=params['sentence_audio_file'],
                        speaker_wav=params['voice_path'],
                        **generation_params
                    )
        elif params['tts_model'] == 'fairseq':
            with torch.no_grad():
                params['tts'].tts_with_vc_to_file(
                    text=params['sentence'],
                    file_path=params['sentence_audio_file'],
                    speaker_wav=params['voice_path'],
                    split_sentences=session['enable_text_splitting']
                )
        if session['device'] == 'cuda':
            torch.cuda.empty_cache()
        collected = gc.collect()
        if os.path.exists(params['sentence_audio_file']):
            return True
        print(f"Cannot create {params['sentence_audio_file']}")
        return False
    except Exception as e:
        raise DependencyError(e)
        
def combine_audio_sentences(chapter_audio_file, start, end, session):
    try:
        chapter_audio_file = os.path.join(session['chapters_dir'], chapter_audio_file)
        combined_audio = AudioSegment.empty()  
        # Get all audio sentence files sorted by their numeric indices
        sentence_files = [f for f in os.listdir(session['chapters_dir_sentences']) if f.endswith(".wav")]
        sentences_dir_ordered = sorted(sentence_files, key=lambda x: int(re.search(r'\d+', x).group()))
        # Filter the files in the range [start, end]
        selected_files = [
            f for f in sentences_dir_ordered 
            if start <= int(''.join(filter(str.isdigit, os.path.basename(f)))) <= end
        ]
        for f in selected_files:
            if session['cancellation_requested']:
                #stop_and_detach_tts(params['tts'])
                print('Cancel requested')
                return False
            if session['cancellation_requested']:
                msg = 'Cancel requested'
                raise ValueError(msg)
            audio_segment = AudioSegment.from_file(os.path.join(session['chapters_dir_sentences'],f), format=audioproc_format)
            combined_audio += audio_segment
        combined_audio.export(chapter_audio_file, format=audioproc_format)
        print(f'Combined audio saved to {chapter_audio_file}')
        return True
    except Exception as e:
        raise DependencyError(e)


def combine_audio_chapters(session):
    def sort_key(chapter_file):
        numbers = re.findall(r'\d+', chapter_file)
        return int(numbers[0]) if numbers else 0
        
    def assemble_audio():
        try:
            combined_audio = AudioSegment.empty()
            batch_size = 256
            # Process the chapter files in batches
            for i in range(0, len(chapter_files), batch_size):
                batch_files = chapter_files[i:i + batch_size]
                batch_audio = AudioSegment.empty()  # Initialize an empty AudioSegment for the batch
                # Sequentially append each file in the current batch to the batch_audio
                for chapter_file in batch_files:
                    if session['cancellation_requested']:
                        print('Cancel requested')
                        return False
                    audio_segment = AudioSegment.from_wav(os.path.join(session['chapters_dir'],chapter_file))
                    batch_audio += audio_segment
                combined_audio += batch_audio
            combined_audio.export(assembled_audio, format=audioproc_format)
            print(f'Combined audio saved to {assembled_audio}')
            return True
        except Exception as e:
            raise DependencyError(e)

    def generate_ffmpeg_metadata():
        try:
            if session['cancellation_requested']:
                print('Cancel requested')
                return False
            ffmpeg_metadata = ';FFMETADATA1\n'        
            if session['metadata'].get('title'):
                ffmpeg_metadata += f"title={session['metadata']['title']}\n"            
            if session['metadata'].get('creator'):
                ffmpeg_metadata += f"artist={session['metadata']['creator']}\n"
            if session['metadata'].get('language'):
                ffmpeg_metadata += f"language={session['metadata']['language']}\n\n"
            if session['metadata'].get('publisher'):
                ffmpeg_metadata += f"publisher={session['metadata']['publisher']}\n"              
            if session['metadata'].get('description'):
                ffmpeg_metadata += f"description={session['metadata']['description']}\n"
            if session['metadata'].get('published'):
                # Check if the timestamp contains fractional seconds
                if '.' in session['metadata']['published']:
                    # Parse with fractional seconds
                    year = datetime.strptime(session['metadata']['published'], '%Y-%m-%dT%H:%M:%S.%f%z').year
                else:
                    # Parse without fractional seconds
                    year = datetime.strptime(session['metadata']['published'], '%Y-%m-%dT%H:%M:%S%z').year
            else:
                # If published is not provided, use the current year
                year = datetime.now().year
            ffmpeg_metadata += f'year={year}\n'
            if session['metadata'].get('identifiers') and isinstance(session['metadata'].get('identifiers'), dict):
                isbn = session['metadata']['identifiers'].get('isbn', None)
                if isbn:
                    ffmpeg_metadata += f'isbn={isbn}\n'  # ISBN
                mobi_asin = session['metadata']['identifiers'].get('mobi-asin', None)
                if mobi_asin:
                    ffmpeg_metadata += f'asin={mobi_asin}\n'  # ASIN                   
            start_time = 0
            for index, chapter_file in enumerate(chapter_files):
                if session['cancellation_requested']:
                    msg = 'Cancel requested'
                    raise ValueError(msg)

                duration_ms = len(AudioSegment.from_wav(os.path.join(session['chapters_dir'],chapter_file)))
                ffmpeg_metadata += f'[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_time}\n'
                ffmpeg_metadata += f'END={start_time + duration_ms}\ntitle=Chapter {index + 1}\n'
                start_time += duration_ms
            # Write the metadata to the file
            with open(metadata_file, 'w', encoding='utf-8') as f:
                f.write(ffmpeg_metadata)
            return True
        except Exception as e:
            raise DependencyError(e)

    def export_audio():
        try:
            if session['cancellation_requested']:
                print('Cancel requested')
                return False
            ffmpeg_cover = None
            if session['script_mode'] == DOCKER_UTILS:
                docker_dir = os.path.basename(session['process_dir'])
                ffmpeg_combined_audio = f'/files/{docker_dir}/' + os.path.basename(assembled_audio)
                ffmpeg_metadata_file = f'/files/{docker_dir}/' + os.path.basename(metadata_file)
                ffmpeg_final_file = f'/files/{docker_dir}/' + os.path.basename(docker_final_file)           
                if session['cover'] is not None:
                    ffmpeg_cover = f'/files/{docker_dir}/' + os.path.basename(session['cover'])                   
                ffmpeg_cmd = ['ffmpeg', '-i', ffmpeg_combined_audio, '-i', ffmpeg_metadata_file]
            else:
                ffmpeg_combined_audio = assembled_audio
                ffmpeg_metadata_file = metadata_file
                ffmpeg_final_file = final_file
                if session['cover'] is not None:
                    ffmpeg_cover = session['cover']                    
                ffmpeg_cmd = [shutil.which('ffmpeg'), '-i', ffmpeg_combined_audio, '-i', ffmpeg_metadata_file]
            if ffmpeg_cover is not None:
                ffmpeg_cmd += ['-i', ffmpeg_cover, '-map', '0:a', '-map', '2:v']
            else:
                ffmpeg_cmd += ['-map', '0:a'] 
            ffmpeg_cmd += ['-map_metadata', '1', '-c:a', 'aac', '-b:a', '128k', '-ar', '44100']           
            if ffmpeg_cover is not None:
                if ffmpeg_cover.endswith('.png'):
                    ffmpeg_cmd += ['-c:v', 'png', '-disposition:v', 'attached_pic']  # PNG cover
                else:
                    ffmpeg_cmd += ['-c:v', 'copy', '-disposition:v', 'attached_pic']  # JPEG cover (no re-encoding needed)                    
            if ffmpeg_cover is not None and ffmpeg_cover.endswith('.png'):
                ffmpeg_cmd += ['-pix_fmt', 'yuv420p']            
            ffmpeg_cmd += ['-movflags', '+faststart', '-y', ffmpeg_final_file]
            if session['script_mode'] == DOCKER_UTILS:
                try:
                    container = session['client'].containers.run(
                        docker_utils_image,
                        command=ffmpeg_cmd,
                        volumes={session['process_dir']: {'bind': f'/files/{docker_dir}', 'mode': 'rw'}},
                        remove=True,
                        detach=False,
                        stdout=True,
                        stderr=True
                    )
                    print(container.decode('utf-8'))
                    if shutil.copy(docker_final_file, final_file):
                        return True
                    return False
                except docker.errors.ContainerError as e:
                    raise DependencyError(e)
                except docker.errors.ImageNotFound as e:
                    raise DependencyError(e)
                except docker.errors.APIError as e:
                    raise DependencyError(e)
            else:
                try:
                    subprocess.run(ffmpeg_cmd, env={}, check=True)
                    return True
                except subprocess.CalledProcessError as e:
                    raise DependencyError(e)
 
        except Exception as e:
            raise DependencyError(e)

    try:
        chapter_files = [f for f in os.listdir(session['chapters_dir']) if f.endswith(".wav")]
        chapter_files = sorted(chapter_files, key=lambda x: int(re.search(r'\d+', x).group()))
        assembled_audio = os.path.join(session['process_dir'], session['metadata']['title'] + '.' + audioproc_format)
        metadata_file = os.path.join(session['process_dir'], 'metadata.txt')
        if assemble_audio():
            if generate_ffmpeg_metadata():
                final_name = session['metadata']['title'] + '.' + audiobook_format
                docker_final_file = os.path.join(session['process_dir'], final_name)
                final_file = os.path.join(session['audiobooks_dir'], final_name)       
                if export_audio():
                    return final_file
        return None
    except Exception as e:
        raise DependencyError(e)        

def replace_roman_numbers(text):
    def roman_to_int(s):
        try:
            roman = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000,
                     'IV': 4, 'IX': 9, 'XL': 40, 'XC': 90, 'CD': 400, 'CM': 900}   
            i = 0
            num = 0   
            # Iterate over the string to calculate the integer value
            while i < len(s):
                # Check for two-character numerals (subtractive combinations)
                if i + 1 < len(s) and s[i:i+2] in roman:
                    num += roman[s[i:i+2]]
                    i += 2
                else:
                    # Add the value of the single character
                    num += roman[s[i]]
                    i += 1   
            return num
        except Exception as e:
            return s

    roman_chapter_pattern = re.compile(
        r'\b(chapter|volume|chapitre|tome|capitolo|capítulo|volumen|Kapitel|глава|том|κεφάλαιο|τόμος|capitul|poglavlje)\s'
        r'(M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})|[IVXLCDM]+)\b',
        re.IGNORECASE
    )

    roman_numerals_with_period = re.compile(
        r'^(M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})|[IVXLCDM])\.+'
    )

    def replace_chapter_match(match):
        chapter_word = match.group(1)
        roman_numeral = match.group(2)
        integer_value = roman_to_int(roman_numeral.upper())
        return f'{chapter_word.capitalize()} {integer_value}'

    def replace_numeral_with_period(match):
        roman_numeral = match.group(1)
        integer_value = roman_to_int(roman_numeral)
        return f'{integer_value}.'

    text = roman_chapter_pattern.sub(replace_chapter_match, text)
    text = roman_numerals_with_period.sub(replace_numeral_with_period, text)
    return text

'''
def stop_and_detach_tts(tts=None):
    if tts is not None:
        if next(tts.parameters()).is_cuda:
            tts.to('cpu')
        del tts
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
'''

def delete_unused_tmp_dirs(web_dir, days, session):
    dir_array = [
        tmp_dir,
        web_dir,
        os.path.join(models_dir, '__sessions'),
        os.path.join(voices_dir, '__sessions')
    ]
    current_user_folders = {
        f"ebook-{session['id']}",
        f"web-{session['id']}",
        f"voice-{session['id']}",
        f"model-{session['id']}"
    }
    current_time = time.time()
    threshold_time = current_time - (days * 24 * 60 * 60)  # Convert days to seconds
    for dir_path in dir_array:
        if not os.path.exists(dir_path) or not os.path.isdir(dir_path):
            continue  
        for dir in os.listdir(dir_path):
            if dir in current_user_folders:
                continue           
            full_dir_path = os.path.join(dir_path, dir)  # Use a new variable
            if not os.path.isdir(full_dir_path):
                continue
            try:
                dir_mtime = os.path.getmtime(full_dir_path)
                dir_ctime = os.path.getctime(full_dir_path)
                if dir_mtime < threshold_time and dir_ctime < threshold_time:
                    shutil.rmtree(full_dir_path)
                    print(f"Deleted unmatched or old directory: {full_dir_path}")
            except Exception as e:
                print(f"Error deleting {full_dir_path}: {e}")

def compare_file_metadata(f1, f2):
    if os.path.getsize(f1) != os.path.getsize(f2):
        return False
    if os.path.getmtime(f1) != os.path.getmtime(f2):
        return False
    return True
    
def get_compatible_tts_engines(language):
    compatible_engines = [
        engine_type for engine_type in models.keys()
        if language in language_tts.get(engine_type, {})
    ]
    return compatible_engines

def convert_ebook(args):
    try:
        global is_gui_process, context        
        error = None

        if args['language'] is not None and args['language'] in language_mapping.keys():
            
            try:
                if len(args['language']) == 2:
                    lang_array = languages.get(part1=args['language'])
                    if lang_array:
                        args['language'] = lang_array.part3
                        args['language_iso1'] = lang_array.part1
                elif len(args['language']) == 3:
                    lang_array = languages.get(part3=args['language'])
                    if lang_array:
                        args['language'] = lang_array.part3
                        args['language_iso1'] = lang_array.part1 
                else:
                    args['language_iso1'] = None
            except Exception as e:
                pass
            
            is_gui_process = args['is_gui_process']
            id = args['session'] if args['session'] is not None else str(uuid.uuid4())
            session = context.get_session(id)
            session['script_mode'] = args['script_mode'] if args['script_mode'] is not None else NATIVE   
            session['src'] = args['ebook']
            session['device'] = args['device']
            session['language'] = args['language']
            session['language_iso1'] = args['language_iso1']
            session['custom_model'] = args['custom_model'] if not is_gui_process or args['custom_model'] is None else os.path.join(session['custom_model_dir'], args['custom_model'])
            session['fine_tuned'] = args['fine_tuned']
            session['temperature'] =  args['temperature']
            session['length_penalty'] = args['length_penalty']
            session['repetition_penalty'] = args['repetition_penalty']
            session['top_k'] =  args['top_k']
            session['top_p'] = args['top_p']
            session['speed'] = args['speed']
            session['enable_text_splitting'] = args['enable_text_splitting']
            session['audiobooks_dir'] = args['audiobooks_dir']
            session['voice'] = args['voice']

            if not os.path.splitext(args['ebook'])[1]:
                raise ValueError('The selected ebook file has no extension. Please select a valid file.')

            if not is_gui_process:
                session['voice_dir'] = os.path.join(voices_dir, '__sessions',f"voice-{session['id']}")            
                session['voice'] = args['voice']
                if session['voice'] is not None:
                    os.makedirs(session['voice_dir'], exist_ok=True)
                    normalized_file = normalize_voice_file(session['voice'], session)
                    if normalized_file is not None:
                        session['voice'] = normalized_file
                session['custom_model_dir'] = os.path.join(models_dir, '__sessions',f"model-{session['id']}")
                if session['custom_model'] is not None:
                    os.makedirs(session['custom_model_dir'], exist_ok=True)
                    if analyze_uploaded_file(custom_model_file):
                        model = extract_custom_model(custom_model_file, session)
                        if model is not None:
                            session['custom_model'] = model
                        else:
                            error = f"{model} could not be extracted or mandatory files are missing"
            if error is None:
                if session['script_mode'] == NATIVE:
                    bool, e = check_programs('Calibre', 'calibre', '--version')
                    if not bool:
                        error = f'check_programs() Calibre failed: {e}'
                    bool, e = check_programs('FFmpeg', 'ffmpeg', '-version')
                    if not bool:
                        error = f'check_programs() FFMPEG failed: {e}'
                elif session['script_mode'] == DOCKER_UTILS:
                    session['client'] = docker.from_env()

                if error is None:
                    session['session_dir'] = os.path.join(tmp_dir, f"ebook-{session['id']}")
                    session['process_dir'] = os.path.join(session['session_dir'], f"{hashlib.md5(session['src'].encode()).hexdigest()}")
                    session['chapters_dir'] = os.path.join(session['process_dir'], "chapters")
                    session['chapters_dir_sentences'] = os.path.join(session['chapters_dir'], 'sentences')       
                    if prepare_dirs(args['ebook'], session):
                        session['filename_noext'] = os.path.splitext(os.path.basename(session['src']))[0]
                        if session['device'] == 'cuda':
                            session['device'] = session['device'] if torch.cuda.is_available() else 'cpu'
                            if session['device'] == 'cpu':
                                print('GPU is not available on your device!')
                        elif session['device'] == 'mps':
                            session['device'] = session['device'] if torch.backends.mps.is_available() else 'cpu'
                            if session['device'] == 'cpu':
                                print('MPS is not available on your device!')
                        print(f"Available Processor Unit: {session['device']}")   
                        session['epub_path'] = os.path.join(session['process_dir'], '__' + session['filename_noext'] + '.epub')
                        if convert_to_epub(session):
                            epubBook = epub.read_epub(session['epub_path'], {'ignore_ncx': True})       
                            metadata = dict(session['metadata'])
                            for key, value in metadata.items():
                                data = epubBook.get_metadata('DC', key)
                                if data:
                                    for value, attributes in data:
                                        metadata[key] = value
                            metadata['language'] = session['language']
                            metadata['title'] = os.path.splitext(os.path.basename(session['src']))[0].replace('_',' ') if not metadata['title'] else metadata['title']
                            metadata['creator'] =  False if not metadata['creator'] or metadata['creator'] == 'Unknown' else metadata['creator']
                            session['metadata'] = metadata
                            
                            try:
                                if len(session['metadata']['language']) == 2:
                                    lang_array = languages.get(part1=session['language'])
                                    if lang_array:
                                        session['metadata']['language'] = lang_array.part3     
                            except Exception as e:
                                pass
                           
                            if session['metadata']['language'] != session['language']:
                                error = f"WARNING!!! language selected {session['language']} differs from the EPUB file language {session['metadata']['language']}"
                                print(error)
                            session['cover'] = get_cover(epubBook, session)
                            if session['cover']:
                                session['chapters'] = get_chapters(epubBook, session)
                                if session['chapters']:
                                    if convert_chapters_to_audio(session):
                                        final_file = combine_audio_chapters(session)               
                                        if final_file is not None:
                                            chapters_dirs = [
                                                dir_name for dir_name in os.listdir(session['process_dir'])
                                                if fnmatch.fnmatch(dir_name, "chapters_*") and os.path.isdir(os.path.join(session['process_dir'], dir_name))
                                            ]
                                            if len(chapters_dirs) > 1:
                                                if os.path.exists(session['chapters_dir']):
                                                    shutil.rmtree(session['chapters_dir'])
                                                if os.path.exists(session['epub_path']):
                                                    os.remove(session['epub_path'])
                                                if os.path.exists(session['cover']):
                                                    os.remove(session['cover'])
                                            else:
                                                if os.path.exists(session['process_dir']):
                                                    shutil.rmtree(session['process_dir'])
                                            progress_status = f'Audiobook {os.path.basename(final_file)} created!'
                                            if not is_gui_process:
                                                print(f'*********** Session: {id}', '************* Store it in case of interruption or crash you can resume the conversion')
                                            return progress_status, final_file 
                                        else:
                                            error = 'combine_audio_chapters() error: final_file not created!'
                                    else:
                                        error = 'convert_chapters_to_audio() failed!'
                                else:
                                    error = 'get_chapters() failed!'
                            else:
                                error = 'get_cover() failed!'
                        else:
                            error = 'convert_to_epub() failed!'
                    else:
                        error = f"Temporary directory {session['process_dir']} not removed due to failure."
        else:
            error = f"Language {args['language']} is not supported."
        if session['cancellation_requested']:
            error = 'Cancelled'
        print(error)
        return error, None
    except Exception as e:
        print(f'convert_ebook() Exception: {e}')
        return e, None

def get_all_ip_addresses():
    ip_addresses = []
    for interface, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family == socket.AF_INET:
                ip_addresses.append(address.address)
            elif address.family == socket.AF_INET6:
                ip_addresses.append(address.address)  
    return ip_addresses

def web_interface(args):
    script_mode = args['script_mode']
    is_gui_process = args['is_gui_process']
    is_gui_shared = args['share']
    audiobooks_dir = None
    ebook_src = None
    audiobook_file = None
    language_options = [
        (
            f"{details['name']} - {details['native_name']}" if details['name'] != details['native_name'] else details['name'],
            lang
        )
        for lang, details in language_mapping.items()
    ]
    custom_model_options = []
    voice_options = []
    tts_engine_options = []
    fine_tuned_options = []
    audiobook_options = []

    theme = gr.themes.Origin(
        primary_hue='amber',
        secondary_hue='green',
        neutral_hue='gray',
        radius_size='lg',
        font_mono=['JetBrains Mono', 'monospace', 'Consolas', 'Menlo', 'Liberation Mono']
    )

    def process_cleanup(state):
        try:
            print('***************PROCESS CLEANING REQUESTED*****************')
            if state['id'] in context.sessions:
                del context.sessions[state['id']]
        except Exception as e:
            raise DependencyError(e)

    with gr.Blocks(theme=theme, delete_cache=(86400, 86400)) as interface:
        gr.HTML(
            '''
            <style>
                .svelte-1xyfx7i.center.boundedheight.flex{
                    height: 120px !important;
                }
                .block.svelte-5y6bt2 {
                    padding: 10px !important;
                    margin: 0 !important;
                    height: auto !important;
                    font-size: 16px !important;
                }
                .wrap.svelte-12ioyct {
                    padding: 0 !important;
                    margin: 0 !important;
                    font-size: 12px !important;
                }
                .block.svelte-5y6bt2.padded {
                    height: auto !important;
                    padding: 10px !important;
                }
                .block.svelte-5y6bt2.padded.hide-container {
                    height: auto !important;
                    padding: 0 !important;
                }
                .waveform-container.svelte-19usgod {
                    height: 58px !important;
                    overflow: hidden !important;
                    padding: 0 !important;
                    margin: 0 !important;
                }
                .component-wrapper.svelte-19usgod {
                    height: 110px !important;
                }
                .timestamps.svelte-19usgod {
                    display: none !important;
                }
                .controls.svelte-ije4bl {
                    padding: 0 !important;
                    margin: 0 !important;
                }
                .svelte-1b742ao.center.boundedheight.flex {
                    height: 140px !important;
                }
                #component-8, #component-13, #component-26 {
                    height: 140px !important;
                }
                #component-10 {
                    height: 127px !important;
                }
                #component-51, #component-52, #component-53 {
                    height: 80px !important;
                }
                #component-14 span[data-testid="block-info"],
                #component-27 span[data-testid="block-info"] {
                    display: none;
                }
            </style>
            <script>
            setInterval(() => {
                const data = window.localStorage.getItem('data');
                if(data){
                    const obj = JSON.parse(data);
                    if(typeof(obj.id) != 'undefined'){
                        if(obj.id != null && obj.id != ""){
                            fetch('/api/heartbeat', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ 'id': obj.id })
                            });
                        }
                    }
                }
            }, 5000);
            </script>
            '''
        )
        gr.Markdown(
            f'''
            <h1 style="line-height: 0.7">Ebook2Audiobook v{version}</h1>
            <a href="https://github.com/DrewThomasson/ebook2audiobook" target="_blank" style="line-height:0">https://github.com/DrewThomasson/ebook2audiobook</a>
            <div style="line-height: 1.3;">
                Multiuser, multiprocessing tasks on a geo cluster to share the conversion to the Grid<br/>
                Convert eBooks into immersive audiobooks with realistic voice TTS models.<br/>
            </div>
            '''
        )
        with gr.Tabs():
            gr_tab_main = gr.TabItem('Main Parameters')
            with gr_tab_main:
                with gr.Row():
                    with gr.Column(scale=3):
                        with gr.Group():
                            gr_ebook_file = gr.Files(
                                label='EBook File (.epub, .mobi, .azw3, fb2, lrf, rb, snb, tcr, .pdf, .txt, .rtf, doc, .docx, .html, .odt, .azw)',
                                file_types=['.epub', '.mobi', '.azw3', 'fb2', 'lrf', 'rb', 'snb', 'tcr', '.pdf', '.txt', '.rtf', 'doc', '.docx', '.html', '.odt', '.azw'],
                                file_count="single"
                            )
                        with gr.Group():
                            gr_language = gr.Dropdown(label='Language', choices=language_options, value=default_language_code, type='value', interactive=True)
                        with gr.Group():
                            gr_voice_file = gr.File(label='*Cloning Voice (a .wav 24khz for XTTS base model and 16khz for FAIRSEQ base model, no more than 6 sec)', file_types=['.wav'], value=None, visible=interface_component_options['gr_voice_file'])
                            gr_voice_list = gr.Dropdown(label='', choices=voice_options, type='value', interactive=True)
                            gr.Markdown('<p>&nbsp;&nbsp;* Optional</p>')
                        with gr.Group():
                            gr_device = gr.Radio(label='Processor Unit', choices=[('CPU','cpu'), ('GPU','cuda'), ('MPS','mps')], value=default_device)
                    with gr.Column(scale=3):
                        with gr.Group():
                            gr_tts_engine_list = gr.Dropdown(label='TTS Base', choices=tts_engine_options, type='value', interactive=True)
                            gr_fine_tuned_list = gr.Dropdown(label='Fine Tuned Models', choices=fine_tuned_options, type='value', interactive=True)
                        gr_group_custom_model = gr.Group(visible=interface_component_options['gr_group_custom_model'])
                        with gr_group_custom_model:
                            gr_custom_model_file = gr.File(label='*Custom XTTS Model (a .zip containing config.json, vocab.json, model.pth, ref.wav)', value=None, file_types=['.zip'])
                            gr_custom_model_list = gr.Dropdown(label='', choices=custom_model_options, type='value', interactive=True)
                            gr.Markdown('<p>&nbsp;&nbsp;* Optional</p>')
                        with gr.Group():
                            gr_session = gr.Textbox(label='Session')
                        gr_outpot_format_list = gr.Dropdown(label='Output format', choices=[('M4B','m4b'), ('MP3','mp3'), ('OPUS','opus')], type='value', interactive=True)
            gr_tab_preferences = gr.TabItem('Fine Tuned Parameters', visible=interface_component_options['gr_tab_preferences'])
            with gr_tab_preferences:
                gr.Markdown(
                    '''
                    ### Customize Audio Generation Parameters
                    Adjust the settings below to influence how the audio is generated. You can control the creativity, speed, repetition, and more.
                    '''
                )
                gr_temperature = gr.Slider(
                    label='Temperature', 
                    minimum=0.1, 
                    maximum=10.0, 
                    step=0.1, 
                    value=tts_default_settings['temperature'],
                    info='Higher values lead to more creative, unpredictable outputs. Lower values make it more monotone.'
                )
                gr_length_penalty = gr.Slider(
                    label='Length Penalty', 
                    minimum=0.5, 
                    maximum=10.0, 
                    step=0.1, 
                    value=tts_default_settings['length_penalty'], 
                    info='Penalize longer sequences. Higher values produce shorter outputs. Not applied to custom models.'
                )
                gr_repetition_penalty = gr.Slider(
                    label='Repetition Penalty', 
                    minimum=1.0, 
                    maximum=10.0, 
                    step=0.1, 
                    value=tts_default_settings['repetition_penalty'], 
                    info='Penalizes repeated phrases. Higher values reduce repetition.'
                )
                gr_top_k = gr.Slider(
                    label='Top-k Sampling', 
                    minimum=10, 
                    maximum=100, 
                    step=1, 
                    value=tts_default_settings['top_k'], 
                    info='Lower values restrict outputs to more likely words and increase speed at which audio generates.'
                )
                gr_top_p = gr.Slider(
                    label='Top-p Sampling', 
                    minimum=0.1, 
                    maximum=1.0, 
                    step=.01, 
                    value=tts_default_settings['top_p'], 
                    info='Controls cumulative probability for word selection. Lower values make the output more predictable and increase speed at which audio generates.'
                )
                gr_speed = gr.Slider(
                    label='Speed', 
                    minimum=0.5, 
                    maximum=3.0, 
                    step=0.1, 
                    value=tts_default_settings['speed'], 
                    info='Adjusts how fast the narrator will speak.'
                )
                gr_enable_text_splitting = gr.Checkbox(
                    label='Enable Text Splitting', 
                    value=tts_default_settings['enable_text_splitting'],
                    info='Splits long texts into sentences to generate audio in chunks. Useful for very long inputs.'
                )
        gr_state = gr.State(value={"hash": None})
        gr_read_data = gr.JSON(visible=False)
        gr_write_data = gr.JSON(visible=False)
        gr_conversion_progress = gr.Textbox(label='Progress')
        gr_convert_btn = gr.Button('Convert', variant='primary', interactive=False)
        gr_audio_player = gr.Audio(label='Listen', type='filepath', show_download_button=False, container=True, visible=False)
        gr_audiobook_list = gr.Dropdown(label='Audiobooks', choices=audiobook_options, type='value', interactive=True)
        gr_audiobook_link = gr.File(label='Download')
        gr_modal_html = gr.HTML()

        def show_modal(message):
            return f'''
            <style>
                .modal {{
                    display: none; /* Hidden by default */
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background-color: rgba(0, 0, 0, 0.5);
                    z-index: 9999;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                }}
                .modal-content {{
                    background-color: #333;
                    padding: 20px;
                    border-radius: 8px;
                    text-align: center;
                    max-width: 300px;
                    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.5);
                    border: 2px solid #FFA500;
                    color: white;
                    font-family: Arial, sans-serif;
                    position: relative;
                }}
                .modal-content p {{
                    margin: 10px 0;
                }}
                /* Spinner */
                .spinner {{
                    margin: 15px auto;
                    border: 4px solid rgba(255, 255, 255, 0.2);
                    border-top: 4px solid #FFA500;
                    border-radius: 50%;
                    width: 30px;
                    height: 30px;
                    animation: spin 1s linear infinite;
                }}
                @keyframes spin {{
                    0% {{ transform: rotate(0deg); }}
                    100% {{ transform: rotate(360deg); }}
                }}
            </style>
            <div id="custom-modal" class="modal">
                <div class="modal-content">
                    <p>{message}</p>
                    <div class="spinner"></div> <!-- Spinner added here -->
                </div>
            </div>
            '''

        def hide_modal():
            return ''

        def restore_interface(id):
            try:
                session = context.get_session(id)
                return (
                    gr.update(value=session['src']), gr.update(choices=voice_options, value=session['voice']), gr.update(value=session['device']), 
                    gr.update(value=session['language']), gr.update(choices=tts_engine_options, value=session['tts_engine']), gr.update(choices=fine_tuned_options, value=session['fine_tuned']), 
                    gr.update(choices=custom_model_options, value=session['custom_model']), gr.update(choices=audiobook_options, value=session['audiobook']), 
                    gr.update(value=session['temperature']), gr.update(value=session['length_penalty']), gr.update(value=session['repetition_penalty']), 
                    gr.update(value=session['top_k']), gr.update(value=session['top_p']), gr.update(value=session['speed']), 
                    gr.update(value=session['enable_text_splitting']), gr.update(active=True)
                )
            except Exception as e:
                raise DependencyError(e)

        def refresh_interface(id):
            session = context.get_session(id)
            session['status'] = None
            audiobook_options = [
                (f, os.path.join(session['audiobooks_dir'], f))
                for f in os.listdir(session['audiobooks_dir'])
            ]
            audiobook_options.sort(
                key=lambda x: os.path.getmtime(x[1]),
                reverse=True
            )
            if len(audiobook_options) > 0:
                session['audiobook'] = audiobook_options[0][1]
            return gr.update('Convert', variant='primary', interactive=False), gr.update(value=None), gr.update(value=session['audiobook']), gr.update(choices=audiobook_options, value=session['audiobook']), gr.update(value=session['audiobook'])

        def change_gr_audiobook_list(audiobook, id):
            session = context.get_session(id)
            if audiobooks_dir is not None:
                session['audiobook'] = audiobook
                if audiobook:
                    link = os.path.join(audiobooks_dir, audiobook)
                    return gr.update(value=link), gr.update(value=link), gr.update(visible=True)
            return gr.update(), gr.update(), gr.update(visible=False)
            
        def update_convert_btn(upload_file=None, custom_model_file=None, session=None):
            if session is None:
                yield gr.update(variant='primary', interactive=False)
                return
            else:
                if hasattr(upload_file, 'name') and not hasattr(custom_model_file, 'name'):
                    yield gr.update(variant='primary', interactive=True)
                else:
                    yield gr.update(variant='primary', interactive=False)   
                return

        async def change_gr_ebook_file(f, id):
            session = context.get_session(id)
            if f is None:
                if session['status'] == 'converting':
                    session['cancellation_requested'] = True    
                    session['src'] = None
                    yield show_modal('Cancellation requested, please wait...')
                    return
            session['cancellation_requested'] = False
            session['src'] = f
            yield hide_modal()
            return

        def change_gr_voice_file(f):
            if f is None:
                return gr.update()
            if len(voice_options) > max_custom_voices:
                error = f'You are allowed to upload a max of {max_custom_voices} voices'    
                return gr.update(value=error)
            else:
                return gr.update()

        def convert_gr_voice_file(f, id):
            try:
                if f is None:
                    return gr.update(), gr.update(), gr.update(value='')
                session = context.get_session(id)
                print(session)
                normalized_file = normalize_voice_file(f, session)
                if normalized_file is None:
                    error = 'Voice file cannot be normalized!'
                else:
                    session['voice'] = normalized_file
                    return gr.update(value=None), update_gr_voice_list(id), gr.update(value=f"Voice {os.path.basename(session['voice'])} added to the voices list")
            except Exception as e:
                error = f'convert_gr_voice_file() error: {e}'
            return gr.update(), gr.update(), gr.update(value=error)

        def change_gr_voice_list(selected, id):
            session = context.get_session(id)
            session['voice'] = next((value for label, value in voice_options if value == selected), None)

        def update_gr_voice_list(id):
            nonlocal voice_options
            session = context.get_session(id)
            voice_options = [('None', None)] + [
                (os.path.splitext(f)[0], os.path.join(session['voice_dir'], f))
                for f in os.listdir(session['voice_dir'])
                if f.endswith('.wav')
            ]
            current_voice = session['voice']
            return gr.update(choices=voice_options, value=current_voice)
        
        def change_gr_device(device, id):
            session = context.get_session(id)
            session['device'] = device

        def change_gr_language(selected, id):
            nonlocal custom_model_options, tts_engine_options, fine_tuned_options
            session = context.get_session(id)
            if selected == 'zzzz':
                new_language_code = default_language_code
            else:
                new_language_code = selected
            session['language'] = new_language_code
            custom_model_tts_dir = check_custom_model_tts(id)
            custom_model_options = [('None', None)] + [
                (os.path.basename(os.path.join(custom_model_tts_dir, dir)), os.path.join(custom_model_tts_dir, dir))
                for dir in os.listdir(custom_model_tts_dir)
                if os.path.isdir(os.path.join(custom_model_tts_dir, dir))
            ]
            custom_model = session['custom_model'] if session['custom_model'] in [option[1] for option in custom_model_options] else custom_model_options[0][1]
            tts_engine_options = get_compatible_tts_engines(session['language'])
            tts_engine = session['tts_engine'] if session['tts_engine'] in tts_engine_options else tts_engine_options[0]
            fine_tuned_options = [
                name for name, details in models.get(tts_engine,{}).items()
                if details.get('lang') == 'multi' or details.get('lang') == session['language']
            ]
            fine_tuned = session['fine_tuned'] if session['fine_tuned'] in fine_tuned_options else fine_tuned_options[0]
            voice_lang_dir = session['language'] if session['language'] != 'con' else 'con-'  # Bypass Windows CON reserved name
            voice_file_pattern = '*_16khz.wav' if session['tts_engine'] == 'fairseq' else '*_24khz.wav'
            voice_options = [
                (os.path.splitext(f)[0], os.path.join(session['voice_dir'], f))
                for f in os.listdir(session['voice_dir'])
                if f.endswith('.wav')
            ]
            voice_options += [
                (os.path.splitext(f.name)[0].replace('_16khz', '').replace('_24khz', ''), str(f))
                for f in Path(os.path.join(voices_dir, voice_lang_dir)).rglob(voice_file_pattern)
            ]
            voice_options = [('None', None)] + sorted(voice_options, key=lambda x: x[0].lower())
            voice = session['voice'] if session['voice'] in [option[1] for option in voice_options] else voice_options[0][1]
            return[
                gr.update(choices=voice_options, value=voice),
                gr.update(choices=custom_model_options, value=custom_model),
                gr.update(choices=tts_engine_options, value=tts_engine),
                gr.update(choices=fine_tuned_options, value=fine_tuned)
            ]

        def check_custom_model_tts(id):
            session = context.get_session(id)
            dir_name = 'xtts'
            if not language_tts['xtts'].get(session['language']):
                dir_name = 'fairseq'
            dir_path = os.path.join(session['custom_model_dir'], dir_name)
            if not os.path.isdir(dir_path):
                os.makedirs(dir_path, exist_ok=True)
            return dir_path
            
        def change_gr_custom_model_file(f):
            if f is None:
                return gr.update()
            if len(custom_model_options) > max_custom_model:
                error = f'You are allowed to upload a max of {max_custom_models} models'    
                return gr.update(value=error)
            else:
                return gr.update()

        def save_gr_custom_model_file(f, id):
            nonlocal custom_model_options
            error = ''
            try:
                session = context.get_session(id)
                if f is None:
                    return gr.update(), gr.update(), gr.update(), gr.update(value='')
                if analyze_uploaded_file(f):
                    model = extract_custom_model(f, session)
                    if model is None:
                        return gr.update(), gr.update(), gr.update(), gr.update(value=f'Cannot extract custom model zip file {os.path.basename(f)}')
                    session['custom_model'] = model
                    custom_model_tts_dir = check_custom_model_tts(id)
                    custom_model_options = [('None', None)] + [
                        (os.path.basename(os.path.join(custom_model_tts_dir, dir)), os.path.join(custom_model_tts_dir, dir))
                        for dir in os.listdir(custom_model_tts_dir)
                        if os.path.isdir(os.path.join(custom_model_tts_dir, dir))
                    ]
                    return[
                        gr.update(value=None),
                        gr.update(choices=custom_model_options, value=session['custom_model']),
                        gr.update(visible=False),
                        gr.update(value=f"{model} added to the custom models list")
                    ]
                else:
                    return gr.update(), gr.update(), gr.update(), gr.update(value=f'{os.path.basename(f)} is not a valid model or some required files are missing')
            except Exception as e:
                error = f'Error: {str(e)}'
                return gr.update(value=None), gr.update(), gr.update(), gr.update(value=error)

        def change_gr_custom_model_list(selected, id):
            session = context.get_session(id)
            session['custom_model'] = next((value for label, value in custom_model_options if value == selected), None)
            visible = False
            if session['custom_model'] is None:
                visible = True
            return gr.update(visible=visible)

        def change_gr_tts_engine_list(engine, id):
            session = context.get_session(id)
            session['tts_engine'] = engine
            fine_tuned_options = [
                name for name, details in models.get(session['tts_engine'], {}).items()
                if details.get('lang') == 'multi' or details.get('lang') == session['language']
            ]
            session['fine_tuned'] = session['fine_tuned'] if session['fine_tuned'] in fine_tuned_options else fine_tuned_options[0]
            if engine == 'xtts' and session['fine_tuned'] == 'std':
                return gr.update(visible=True), gr.update(choices=fine_tuned_options, value=session['fine_tuned'])
            else:
                return gr.update(visible=False), gr.update(choices=fine_tuned_options, value=session['fine_tuned'])
            
        def change_gr_fine_tuned_list(selected, id):
            session = context.get_session(id)
            visible = False
            if selected == 'std':
                visible = True
            session['fine_tuned'] = selected
            return gr.update(visible=visible)

        def change_param(key, val, id):
            session = context.get_session(id)
            session[key] = val           

        def submit_convert_btn(id, device, ebook_file, voice, language, custom_model, temperature, length_penalty,repetition_penalty, top_k, top_p, speed, enable_text_splitting, fine_tuned):
            args = {
                "is_gui_process": is_gui_process,
                "session": id,
                "script_mode": script_mode,
                "device": device.lower(),
                "ebook": ebook_file.name if ebook_file else None,
                "audiobooks_dir": audiobooks_dir,
                "voice": voice,
                "language": language,
                "custom_model": custom_model,
                "temperature": float(temperature),
                "length_penalty": float(length_penalty),
                "repetition_penalty": float(repetition_penalty),
                "top_k": int(top_k),
                "top_p": float(top_p),
                "speed": float(speed),
                "enable_text_splitting": enable_text_splitting,
                "fine_tuned": fine_tuned
            }
            if args["ebook"] is None:
                return gr.update(value='Error: a file is required.')
            try:
                session = context.get_session(id)
                session['status'] = 'converting'
                progress_status, audiobook_file = convert_ebook(args)
                if audiobook_file is None:
                    if session['status'] == 'converting':
                        return gr.update(value='Conversion cancelled.')
                    else:
                        return gr.update(value='Conversion failed.')
                else:
                    return gr.update(value=progress_status)
            except Exception as e:
                return DependencyError(e)

        def restore_session_from_data(data, session):
            for key, value in data.items():
                if key in session:  # Check if the key exists in session
                    if isinstance(value, dict) and isinstance(session[key], dict):
                        restore_session_from_data(value, session[key])
                    else:
                        if value is not None:
                            session[key] = value

        def change_gr_read_data(data, state):
            nonlocal audiobooks_dir, custom_model_options, voice_options, tts_engine_options, fine_tuned_options, audiobook_options
            warning_text_extra = 'Error while loading saved session. Please try to delete your cookies and refresh the page'
            try:
                if data is None:
                    session = context.get_session(str(uuid.uuid4()))
                else:
                    try:
                        if 'id' not in data:
                            data['id'] = str(uuid.uuid4())
                        session = context.get_session(data['id'])
                        restore_session_from_data(data, session)
                        session['cancellation_requested'] = False
                        if session['src'] is not None and not os.path.exists(session['src']):
                            session['src'] = None
                        if session['voice'] is not None:
                            if not os.path.exists(session['voice']):
                                session['voice'] = None
                        if session['custom_model'] is not None:
                            if not os.path.exists(session['custom_model_dir']):
                                session['custom_model'] = None
                        if session['audiobook'] is not None:
                            if not os.path.exists(session['audiobook']):
                                session['audiobook'] = None
                        if session['status'] == 'converting':
                            session['status'] = None
                    except Exception as e:
                        raise DependencyError(e)
                        return gr.update(), gr.update(), gr.update(), gr.update(value=e)
                session['custom_model_dir'] = os.path.join(models_dir, '__sessions', f"model-{session['id']}")
                session['voice_dir'] = os.path.join(voices_dir, '__sessions', f"voice-{session['id']}")
                os.makedirs(session['custom_model_dir'], exist_ok=True)
                os.makedirs(session['voice_dir'], exist_ok=True)             
                custom_model_tts_dir = check_custom_model_tts(session['id'])
                custom_model_options = [('None', None)] + [
                    (os.path.basename(os.path.join(custom_model_tts_dir, dir)), os.path.join(custom_model_tts_dir, dir))
                    for dir in os.listdir(custom_model_tts_dir)
                    if os.path.isdir(os.path.join(custom_model_tts_dir, dir))
                ]
                session['custom_model'] = session['custom_model'] if session['custom_model'] in [option[1] for option in custom_model_options] else custom_model_options[0][1]
                tts_engine_options = get_compatible_tts_engines(session['language'])
                session['tts_engine'] = session['tts_engine'] if session['tts_engine'] in tts_engine_options else tts_engine_options[0]
                fine_tuned_options = [
                    name for name, details in models.get(session['tts_engine'], {}).items()
                    if details.get('lang') == 'multi' or details.get('lang') == session['language']
                ]
                session['fine_tuned'] = session['fine_tuned'] if session['fine_tuned'] in fine_tuned_options else fine_tuned_options[0]
                voice_lang_dir = session['language'] if session['language'] != 'con' else 'con-'  # Bypass Windows CON reserved name
                voice_file_pattern = '*_16khz.wav' if session['tts_engine'] == 'fairseq' else '*_24khz.wav'
                voice_options = [
                    (os.path.splitext(f)[0], os.path.join(session['voice_dir'], f))
                    for f in os.listdir(session['voice_dir'])
                    if f.endswith('.wav')
                ]
                voice_options += [
                    (os.path.splitext(f.name)[0].replace('_16khz', '').replace('_24khz', ''), str(f))
                    for f in Path(os.path.join(voices_dir, voice_lang_dir)).rglob(voice_file_pattern)
                ]
                voice_options = [('None', None)] + sorted(voice_options, key=lambda x: x[0].lower())
                session['voice'] = session['voice'] if session['voice'] in [option[1] for option in voice_options] else voice_options[0][1]
                if is_gui_shared:
                    warning_text_extra = f' Note: access limit time: {interface_shared_tmp_expire} days'
                    audiobooks_dir = os.path.join(audiobooks_gradio_dir, f"web-{session['id']}")
                    session['audiobooks_dir'] = audiobooks_dir
                    delete_unused_tmp_dirs(audiobooks_gradio_dir, interface_shared_tmp_expire, session)
                else:
                    warning_text_extra = f' Note: if no activity is detected after {tmp_expire} days, your session will be cleaned up.'
                    audiobooks_dir = os.path.join(audiobooks_host_dir, f"web-{session['id']}")
                    session['audiobooks_dir'] = audiobooks_dir
                    delete_unused_tmp_dirs(audiobooks_host_dir, tmp_expire, session)
                if not os.path.exists(session['audiobooks_dir']):
                    os.makedirs(session['audiobooks_dir'], exist_ok=True)
                audiobook_options = [
                    (f, os.path.join(session['audiobooks_dir'], f))
                    for f in os.listdir(session['audiobooks_dir'])
                ]
                audiobook_options.sort(
                    key=lambda x: os.path.getmtime(x[1]),
                    reverse=True
                )
                session['audiobook'] = session['audiobook'] if session['audiobook'] in [option[1] for option in audiobook_options] else None
                if len(audiobook_options) > 0:
                    if not session['audiobook']:
                        session['audiobook'] = audiobook_options[0][1]
                previous_hash = state['hash']
                new_hash = hash_proxy_dict(MappingProxyType(session))
                state['hash'] = new_hash
                session_dict = proxy_to_dict(session)
                return gr.update(value=session_dict), gr.update(value=state), gr.update(value=session['id']), gr.update(value=warning_text_extra)
            except Exception as e:
                raise DependencyError(e)
                return gr.update(), gr.update(), gr.update(), gr.update(value=e)

        def save_session(id, state):
            try:
                if id:
                    if id in context.sessions:
                        session = context.get_session(id)
                        if session:
                            if session['event'] == 'clear':
                                session_dict = session
                            else:
                                previous_hash = state['hash']
                                new_hash = hash_proxy_dict(MappingProxyType(session))
                                if previous_hash == new_hash:
                                    return gr.update(), gr.update()
                                else:
                                    state['hash'] = new_hash
                                    session_dict = proxy_to_dict(session)
                            return gr.update(value=json.dumps(session_dict, indent=4)), gr.update(value=state)
                return gr.update()
            except Exception as e:
                return gr.update(), gr.update(value=e)
        
        def clear_event(id):
            session = context.get_session(id)
            if session['event'] is not None:
                session['event'] = None
            
        gr_ebook_file.change(
            fn=update_convert_btn,
            inputs=[gr_ebook_file, gr_custom_model_file, gr_session],
            outputs=[gr_convert_btn]
        ).then(
            fn=change_gr_ebook_file,
            inputs=[gr_ebook_file, gr_session],
            outputs=[gr_modal_html]
        )
        gr_voice_file.change(
            fn=change_gr_voice_file,
            inputs=[gr_voice_file],
            outputs=[gr_conversion_progress]
        ).then(
            fn=convert_gr_voice_file,
            inputs=[gr_voice_file, gr_session],
            outputs=[gr_voice_file, gr_voice_list, gr_conversion_progress]
        )
        gr_voice_list.change(
            fn=change_gr_voice_list,
            inputs=[gr_voice_list, gr_session],
            outputs=None
        )
        gr_device.change(
            fn=change_gr_device,
            inputs=[gr_device, gr_session],
            outputs=None
        )
        gr_language.change(
            fn=change_gr_language,
            inputs=[gr_language, gr_session],
            outputs=[gr_voice_list, gr_custom_model_list, gr_tts_engine_list, gr_fine_tuned_list]
        )
        gr_audiobook_list.change(
            fn=change_gr_audiobook_list,
            inputs=[gr_audiobook_list, gr_session],
            outputs=[gr_audiobook_link, gr_audio_player, gr_audio_player]
        )
        gr_custom_model_file.change(
            fn=change_gr_custom_model_file,
            inputs=[gr_custom_model_file],
            outputs=[gr_conversion_progress]
        ).then(
            fn=save_gr_custom_model_file,
            inputs=[gr_custom_model_file, gr_session],
            outputs=[gr_custom_model_file, gr_custom_model_list, gr_fine_tuned_list, gr_conversion_progress]            
        )
        gr_custom_model_list.change(
            fn=change_gr_custom_model_list,
            inputs=[gr_custom_model_list, gr_session],
            outputs=[gr_fine_tuned_list]
        )
        gr_tts_engine_list.change(
            fn=change_gr_tts_engine_list,
            inputs=[gr_tts_engine_list, gr_session],
            outputs=[gr_tab_preferences, gr_fine_tuned_list]
        )
        gr_fine_tuned_list.change(
            fn=change_gr_fine_tuned_list,
            inputs=[gr_fine_tuned_list, gr_session],
            outputs=[gr_group_custom_model]
        )
        ########## Parameters
        gr_temperature.change(
            fn=lambda val, id: change_param('temperature', val, id),
            inputs=[gr_temperature, gr_session],
            outputs=None
        )
        gr_length_penalty.change(
            fn=lambda val, id: change_param('length_penalty', val, id),
            inputs=[gr_length_penalty, gr_session],
            outputs=None
        )
        gr_repetition_penalty.change(
            fn=lambda val, id: change_param('repetition_penalty', val, id),
            inputs=[gr_repetition_penalty, gr_session],
            outputs=None
        )
        gr_top_k.change(
            fn=lambda val, id: change_param('top_k', val, id),
            inputs=[gr_top_k, gr_session],
            outputs=None
        )
        gr_top_p.change(
            fn=lambda val, id: change_param('top_p', val, id),
            inputs=[gr_top_p, gr_session],
            outputs=None
        )
        gr_speed.change(
            fn=lambda val, id: change_param('speed', val, id),
            inputs=[gr_speed, gr_session],
            outputs=None
        )
        gr_enable_text_splitting.change(
            fn=lambda val, id: change_param('enable_text_splitting', val, id),
            inputs=[gr_enable_text_splitting, gr_session],
            outputs=None
        )
        ##########
        # Timer to save session to localStorage
        gr_timer = gr.Timer(10, active=False)
        gr_timer.tick(
            fn=save_session,
            inputs=[gr_session, gr_state],
            outputs=[gr_write_data, gr_state],
        ).then(
            fn=clear_event,
            inputs=[gr_session],
            outputs=None
        )
        gr_convert_btn.click(
            fn=update_convert_btn,
            outputs=[gr_convert_btn]
        ).then(
            fn=submit_convert_btn,
            inputs=[
                gr_session, gr_device, gr_ebook_file, gr_voice_list, gr_language, 
                gr_custom_model_list, gr_temperature, gr_length_penalty,
                gr_repetition_penalty, gr_top_k, gr_top_p, gr_speed, gr_enable_text_splitting, gr_fine_tuned_list
            ],
            outputs=[gr_conversion_progress]
        ).then(
            fn=refresh_interface,
            inputs=[gr_session],
            outputs=[gr_convert_btn, gr_ebook_file, gr_audio_player, gr_audiobook_list, gr_modal_html]
        )
        gr_write_data.change(
            fn=None,
            inputs=[gr_write_data],
            js='''
                (data)=>{
                    if(data){
                        localStorage.clear();
                        if(data['event'] != 'clear'){
                            console.log('save: ', data);
                            window.localStorage.setItem("data", JSON.stringify(data));
                        }
                    }
                }
            '''
        )       
        gr_read_data.change(
            fn=change_gr_read_data,
            inputs=[gr_read_data, gr_state],
            outputs=[gr_write_data, gr_state, gr_session, gr_conversion_progress]
        ).then(
            fn=restore_interface,
            inputs=[gr_session],
            outputs=[
                gr_ebook_file, gr_voice_list, gr_device, gr_language, 
                gr_tts_engine_list, gr_fine_tuned_list, gr_custom_model_list,
                gr_audiobook_list, gr_temperature, gr_length_penalty, gr_repetition_penalty,
                gr_top_k, gr_top_p, gr_speed, gr_enable_text_splitting, gr_timer
            ]
        )
        interface.load(
            fn=None,
            js='''
            () => {
                try{
                    const data = window.localStorage.getItem('data');
                    if(data){
                        const obj = JSON.parse(data);
                        console.log(obj);
                        return obj;
                    }
                }catch(e){
                    console.log('error: ',e)
                }
                return null;
            }
            ''',
            outputs=[gr_read_data]
        )
    try:
        all_ips = get_all_ip_addresses()
        print("Listening on the following IP:")
        print(all_ips)
        interface.queue(default_concurrency_limit=interface_concurrency_limit).launch(server_name=interface_host, server_port=interface_port, share=is_gui_shared)
    except OSError as e:
        print(f'Connection error: {e}')
    except socket.error as e:
        print(f'Socket error: {e}')
    except KeyboardInterrupt:
        print('Server interrupted by user. Shutting down...')
    except Exception as e:
        print(f'An unexpected error occurred: {e}')