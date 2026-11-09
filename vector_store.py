import logging
import os

import chromadb
from logging_config import get_logger

PRIMARY_CHROMA_PATH = "./chroma_db"
FALLBACK_CHROMA_PATH = "./chroma_db_runtime_fallback"
COLLECTION_NAME = "knowledge_base"

_collection = None
_init_attempted = False
_active_path = None
logger = get_logger("vector")


def get_collection():
    global _collection, _init_attempted, _active_path

    if _collection is not None:
        return _collection

    if _init_attempted:
        return None

    _init_attempted = True

    for path in (PRIMARY_CHROMA_PATH, FALLBACK_CHROMA_PATH):
        try:
            os.makedirs(path, exist_ok=True)
            client = chromadb.PersistentClient(path=path)
            _collection = client.get_or_create_collection(name=COLLECTION_NAME)
            _active_path = path
            if path == FALLBACK_CHROMA_PATH:
                logger.warning("Chroma started with fallback path: %s", path)
            return _collection
        except BaseException as exc:
            logger.exception("Failed to initialize Chroma at %s: %s", path, exc)

    logger.error("Chroma vector store is disabled: both primary and fallback initialization failed")
    return None


def get_active_path():
    return _active_path
