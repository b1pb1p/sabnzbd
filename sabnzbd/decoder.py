#!/usr/bin/python3 -OO
# Copyright 2007-2021 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.decoder - article decoder
"""

import logging
import hashlib
import queue
from threading import Thread
from typing import Tuple, List, Optional

import sabnzbd
import sabnzbd.cfg as cfg
from sabnzbd.constants import SABYENC_VERSION_REQUIRED
from sabnzbd.nzbstuff import Article
from sabnzbd.misc import match_str

# Check for correct SABYenc version
SABYENC_VERSION = None
try:
    import sabyenc3

    SABYENC_ENABLED = True
    SABYENC_VERSION = sabyenc3.__version__
    # Verify version to at least match minor version
    if SABYENC_VERSION[:3] != SABYENC_VERSION_REQUIRED[:3]:
        raise ImportError
except ImportError:
    SABYENC_ENABLED = False


class CrcError(Exception):
    def __init__(self, needcrc: int, gotcrc: int, data: bytes):
        super().__init__()
        self.needcrc = needcrc
        self.gotcrc = gotcrc
        self.data = data


class BadYenc(Exception):
    def __init__(self):
        super().__init__()


class Decoder:
    """Implement thread-like coordinator for the decoders"""

    def __init__(self):
        logging.debug("Initializing decoders")
        # Initialize queue and servers
        self.decoder_queue = queue.Queue()

        # Initialize decoders
        self.decoder_workers = []
        for i in range(cfg.num_decoders()):
            self.decoder_workers.append(DecoderWorker(self.decoder_queue))

    def start(self):
        for decoder_worker in self.decoder_workers:
            decoder_worker.start()

    def is_alive(self) -> bool:
        # Check all workers
        for decoder_worker in self.decoder_workers:
            if not decoder_worker.is_alive():
                return False
        return True

    def stop(self):
        # Put multiple to stop all decoders
        for _ in self.decoder_workers:
            self.decoder_queue.put((None, None))

    def join(self):
        # Wait for all decoders to finish
        for decoder_worker in self.decoder_workers:
            try:
                decoder_worker.join()
            except:
                pass

    def process(self, article: Article, raw_data: List[bytes]):
        # We use reported article-size, just like sabyenc does
        sabnzbd.ArticleCache.reserve_space(article.bytes)
        self.decoder_queue.put((article, raw_data))

    def queue_full(self) -> bool:
        # Check if the queue size exceeds the limits
        return self.decoder_queue.qsize() >= sabnzbd.ArticleCache.decoder_cache_article_limit


class DecoderWorker(Thread):
    """The actuall workhorse that handles decoding!"""

    def __init__(self, decoder_queue):
        super().__init__()
        logging.debug("Initializing decoder %s", self.name)

        self.decoder_queue: queue.Queue[Tuple[Optional[Article], Optional[List[bytes]]]] = decoder_queue

    def run(self):
        while 1:
            # Set Article and NzbObject objects to None so references from this
            # thread do not keep the parent objects alive (see #1628)
            decoded_data = raw_data = article = nzo = None
            article, raw_data = self.decoder_queue.get()
            if not article:
                logging.info("Shutting down decoder %s", self.name)
                break

            nzo = article.nzf.nzo
            art_id = article.article

            # Free space in the decoder-queue
            sabnzbd.ArticleCache.free_reserved_space(article.bytes)

            # Keeping track
            article_success = False

            try:
                if nzo.precheck:
                    raise BadYenc

                if sabnzbd.LOG_ALL:
                    logging.debug("Decoding %s", art_id)

                decoded_data = decode(article, raw_data)
                article_success = True

            except MemoryError:
                logging.warning(T("Decoder failure: Out of memory"))
                logging.info("Decoder-Queue: %d", self.decoder_queue.qsize())
                logging.info("Cache: %d, %d, %d", *sabnzbd.ArticleCache.cache_info())
                logging.info("Traceback: ", exc_info=True)
                sabnzbd.Downloader.pause()

                # This article should be fetched again
                sabnzbd.NzbQueue.reset_try_lists(article)
                continue

            except CrcError as crc_error:
                logging.info("CRC Error in %s" % art_id)

                # Continue to the next one if we found new server
                if search_new_server(article):
                    continue

                # Store data, maybe par2 can still fix it
                decoded_data = crc_error.data

            except (BadYenc, ValueError):
                # Handles precheck and badly formed articles
                if nzo.precheck and raw_data and raw_data[0].startswith(b"223 "):
                    # STAT was used, so we only get a status code
                    article_success = True
                else:
                    # Examine headers (for precheck) or body (for download)
                    # Look for DMCA clues (while skipping "X-" headers)
                    # Detect potential UUencode
                    for line in raw_data:
                        lline = line.lower()
                        if b"message-id:" in lline:
                            article_success = True
                        if not lline.startswith(b"x-") and match_str(
                            lline, (b"dmca", b"removed", b"cancel", b"blocked")
                        ):
                            article_success = False
                            logging.info("Article removed from server (%s)", art_id)
                            break
                        if lline.find(b"\nbegin ") >= 0:
                            logme = T("UUencode detected, only yEnc encoding is supported [%s]") % nzo.final_name
                            logging.error(logme)
                            nzo.fail_msg = logme
                            sabnzbd.NzbQueue.end_job(nzo)
                            break

                # Pre-check, proper article found so just register
                if nzo.precheck and article_success and sabnzbd.LOG_ALL:
                    logging.debug("Server %s has article %s", article.fetcher, art_id)
                elif not article_success:
                    # If not pre-check, this must be a bad article
                    if not nzo.precheck:
                        logging.info("Badly formed yEnc article in %s", art_id, exc_info=True)

                    # Continue to the next one if we found new server
                    if search_new_server(article):
                        continue

            except:
                logging.warning(T("Unknown Error while decoding %s"), art_id)
                logging.info("Traceback: ", exc_info=True)

                # Continue to the next one if we found new server
                if search_new_server(article):
                    continue

            if decoded_data:
                # If the data needs to be written to disk due to full cache, this will be slow
                # Causing the decoder-queue to fill up and delay the downloader
                sabnzbd.ArticleCache.save_article(article, decoded_data)

            sabnzbd.NzbQueue.register_article(article, article_success)


def decode(article: Article, raw_data: List[bytes]) -> bytes:
    # Let SABYenc do all the heavy lifting
    decoded_data, yenc_filename, crc, crc_expected, crc_correct = sabyenc3.decode_usenet_chunks(raw_data, article.bytes)

    # Mark as decoded
    article.decoded = True

    # Assume it is yenc
    article.nzf.type = "yenc"

    # Only set the name if it was found and not obfuscated
    if not article.nzf.filename_checked and yenc_filename:
        # Set the md5-of-16k if this is the first article
        if article.lowest_partnum:
            article.nzf.md5of16k = hashlib.md5(decoded_data[:16384]).digest()

        # Try the rename, even if it's not the first article
        # For example when the first article was missing
        article.nzf.nzo.verify_nzf_filename(article.nzf, yenc_filename)

    # CRC check
    if not crc_correct:
        raise CrcError(crc_expected, crc, decoded_data)

    return decoded_data


def search_new_server(article: Article) -> bool:
    """Shorthand for searching new server or else increasing bad_articles"""
    # Continue to the next one if we found new server
    if not article.search_new_server():
        # Increase bad articles if no new server was found
        article.nzf.nzo.increase_bad_articles_counter("bad_articles")
        return False
    return True
