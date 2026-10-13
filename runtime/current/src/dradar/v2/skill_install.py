"""Checksum-pinned install of the packaged candidate skill, with reversible backup."""
from importlib.resources import files
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

def packaged_skill() -> bytes:
    return files('dradar.v2').joinpath('skill/SKILL.md').read_bytes()

