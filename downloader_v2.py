from __future__ import annotations # for forward references in type hints
import subprocess
from typing import Iterator, Optional
import requests
import os
import time
import json 
import re
import logging
from urllib.parse import urlparse
from tqdm import tqdm
from dataclasses import asdict, dataclass, field
from pathlib import Path
from itertools import permutations


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='[%Y-%m-%d %H:%M:%S]', filename='downloader.log')

### Constants
COLLECTION_API = "https://osucollector.com/api/collections/"
MIRROR_API = "https://mirror.hinamizawa.ai/api/v1/hinai/d/" # for search, needs to be followed by ={"include": "all", 'format': 'json'}, and for download needs to be ran with {'noVideo': 1}
PREFS_FILE = 'preferences.json'

# EZ, NF, HT, DC, HR, DT, NC, HD, TR, FL
MODS = ["EZ", "NF", "HT", "DC", "HR", "DT", "NC", "HD", "TR", "FL"]

### Helper functions 

def sanitize_name(filename: str) -> str:
    """Sanitize a string to be a valid filename."""
    # Remove invalid characters and strip whitespace
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f.]', '_', filename)
    # Replace multiple spaces with a single space
    sanitized = re.sub(r'\s+', ' ', sanitized)
    return sanitized

def create_file_name(artist: str, title: str, beatmapset_id: int, MAX: int = 80) -> str:
    """Create a sanitized filename for the beatmapset."""
    name = sanitize_name(f"{artist} - {title}")[:MAX]
    filename = f"{beatmapset_id} - {name}.osz"
    return filename

def check_for_osz(iterable: Iterator[bytes]) -> bool | tuple[bytes, Iterator[bytes]]:
    """Check if the first few bytes of the iterator indicate a valid .osz file (ZIP format)."""
    try:
        first_chunk = next(iterable)
        if first_chunk.startswith(b'PK'):
            return first_chunk, iterable
        return False
    except StopIteration:
        return False

def build_mod_candidates(mods: list[str]) -> list[str]:
    """All possible key orderings for a set of mods, to search against pp_data."""
    mod_set = {m.upper() for m in mods}
    if not mod_set:
        return None
    return ["".join(p) for p in permutations(mod_set)]

def prompt_mods() -> list[str]:
    w_filter = input("Do you want to filter by mods? (y/n): ").lower() == "y"
    if not w_filter:
        return []
    mods = input("Enter mods (comma-separated, e.g., HD,HR,DT): ").strip()
    if not mods:
        return []
    return [m.strip().upper() for m in mods.split(",") if m.strip().upper() in MODS]

def extract_collection_id(raw: str) -> str:
    """Accepts a raw ID or an osucollector.com URL, returns the ID string."""
    if raw.isdigit():
        return raw
    parsed = urlparse(raw)
    if parsed.netloc == "osucollector.com" and parsed.path.startswith("/collections/"):
        return parsed.path.split("/")[2]
    raise ValueError("not a valid collection ID or link")

def prompt_collection_id(last: str = None) -> tuple[str, BeatmapCollection]:
    c_id = None
    while True:
        prompt = f"Enter the collection ID or link (Enter for last: {last}): " if last else "Enter the collection ID or link: "
        raw = input(prompt).strip()
        if raw == "" and last:
            raw = last
        try:
            c_id = extract_collection_id(raw)
            collection = fetch_collection(c_id)
            return c_id, collection
        except ValueError:
            print("Please enter a valid collection ID (integer) or link.")
        except requests.RequestException as e:
            print(f"Failed to fetch collection {c_id}: {e}. Please enter a new one.")


def prompt_star_range() -> tuple[float, float]:
    while True:
        try:
            min_stars = float(input("Enter the minimum difficulty rating (0-99): "))
            max_stars = float(input("Enter the maximum difficulty rating (0-99): "))
        except ValueError:
            print("Please enter numbers.")
            continue
        if min_stars < 0 or max_stars > 99 or min_stars > max_stars:
            print("Range must be within 0-99, and min <= max.")
            continue
        return min_stars, max_stars

def prompt_limit_and_start(Max: int) -> tuple[int, int]:
    while True:
        try:
            limit = int(input(f"Enter the number of maps to download: (Maps: {Max}) "))
        except ValueError:
            print("Please enter a valid integer.")
            continue
        if limit <= 0:
            print("Must be greater than 0.")
            continue
        if limit > Max:
            print(f"Limit exceeds the number of maps in the collection ({Max}). Setting limit to {Max}.")
            limit = Max
    
        start_raw = input("Enter the starting index (press enter for 0): ").strip()
        try:
            start = int(start_raw) if start_raw else 0
        except ValueError:
            print("Starting index must be an integer.")
            continue
        if Max - start < limit:
            print(f"Starting index + limit exceeds the number of maps in the collection ({Max}). Adjusting limit to {Max - start}.")
            limit = Max - start

        return limit, start

def prompt_bpm_range() -> tuple[float, float]:
    w_filter = input("Do you want to filter by BPM? (y/n): ").lower() == "y"
    if not w_filter:
        return 0, 500  # No filtering
    while True:
        try:
            min_bpm = float(input("Enter the minimum BPM (0-500): "))
            max_bpm = float(input("Enter the maximum BPM (0-500): "))
        except ValueError:
            print("Please enter numbers.")
            continue
        if min_bpm < 0 or max_bpm > 500 or min_bpm > max_bpm:
            print("Range must be within 0-500, and min <= max.")
            continue

        return min_bpm, max_bpm

@dataclass
class Preferences:
    min_stars: float = 0.0
    max_stars: float = 99.0
    last_collection: str = None

    @classmethod
    def load(cls) -> Preferences:
        """Load user preferences from a JSON file."""
        if not os.path.exists(PREFS_FILE):
            return cls()
        with open(PREFS_FILE, 'r') as f:
            data = json.load(f)
        return cls(
            min_stars=data.get("MIN", 0.0),
            max_stars=data.get("MAX", 99.0),
            last_collection=data.get("LAST"),
         )

    def save(self):
        with open(PREFS_FILE, 'w') as f:
            json.dump(asdict(self), f, indent=4)


@dataclass
class OsuFilter:
    minstar: Optional[float] = 0
    maxstar: Optional[float] = 10
    minbpm: Optional[float] = 0
    maxbpm: Optional[float] = 500 # ? i think 500 is a good upper limit for bpm, but we can change it if needed
    # Additional filters can be added here as needed
    mods: list[str] = field(default_factory=list)  # List of mods to filter by
    mod_candiates: list[str] = field(default_factory=list, init=False)  # All possible key orderings for the mods

    def __post_init__(self):
        self.mod_candiates = build_mod_candidates(self.mods)

def gather_settings():
    prefs = Preferences.load()

    # Get a valid collection ID from the user
    collection_id, collection = prompt_collection_id(prefs.last_collection)

    use_saved = False
    if prefs.last_collection is not None:  # i.e. a prefs file existed with usable data
        use_saved = input("Use saved star filter preferences? (y/n): ").lower() == "y"

    if use_saved:
        min_stars, max_stars = prefs.min_stars, prefs.max_stars
        print(f"Using saved preferences: MIN={min_stars}, MAX={max_stars}")
    else:
        min_stars, max_stars = prompt_star_range()

    limit, start = prompt_limit_and_start(collection.osucount)

    min_bpm, max_bpm = prompt_bpm_range()

    mods = prompt_mods()

    Preferences(min_stars, max_stars, collection_id).save()  # Save the preferences for next time

    return collection_id, limit, start, min_stars, max_stars, min_bpm, max_bpm, mods, collection

# The following dataclasses will all be gotten from the mirror API
@dataclass
class PPData:
    ar: Optional[float] = None
    bpm: Optional[float] = None
    cs: Optional[float] = None
    hp: Optional[float] = None
    max_combo: Optional[int] = None
    od: Optional[float] = None
    pp: Optional[float] = None
    stars: Optional[float] = None

@dataclass
class PPDataSet:
    mods: dict[str, PPData] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> 'PPDataSet':
        #print(json.dumps(data, indent=4))  # Debugging: print the data being passed to from_dict
        return cls(mods={mod: PPData(**values) for mod, values in data.items()})

@dataclass
class Beatmap:
    id: int
    version: str
    difficulty_rating: float
    pp_data: PPDataSet

    def stats_for_mod(self, candidates: list[str] = None) -> Optional[PPData]:
        """Return the PPData for a specific mod combination, if available."""
        if not candidates:
            # return NM stats if no candidates are provided
            return self.pp_data.mods.get("NM")

        for key in candidates:
            entry = self.pp_data.mods.get(key)
            if entry:
                return entry
        return None

@dataclass
class BeatmapSet:
    success: bool
    download_url: str
    beatmapset_id: int
    source: str
    cascade_ms: int
    
    title: str
    artist: str
    creator: str
    status: str
    bpm: float
    beatmaps: list[Beatmap]

    def filter(self, osu_filter: OsuFilter) -> bool:
        """Check if the beatmapset passes the given filters."""
        candidates = osu_filter.mod_candiates
        for bm in self.beatmaps:
            stats = bm.stats_for_mod(candidates)
            if stats:
                stars, bpm = stats.stars, stats.bpm
                # Now filter by stars and bpm
                if not (osu_filter.minstar <= stars <= osu_filter.maxstar):
                    continue
                if not (osu_filter.minbpm <= bpm <= osu_filter.maxbpm):
                    continue
                return True  # Found a beatmap that passes the filter
            # Check if we should filter by base stats if no mod candidates are provided, as we already have the stars in the Beatmap object and the bpm in the BeatmapSet object
            if not candidates:  # No mod filter, so we can check the base stats
                if not (osu_filter.minstar <= bm.difficulty_rating <= osu_filter.maxstar):
                    continue
                if not (osu_filter.minbpm <= self.bpm <= osu_filter.maxbpm): # 
                    continue
                return True  # Found a beatmap that passes the filter
        return False # No beatmaps passed the filter
            
            

# The following dataclass will be gotten from the osucollector api
@dataclass
class APIBeatmapSet: # later, this will be turned into a BeatmapSet by fetching the data from the mirror API
    id: int
    beatmaps: list[int]

@dataclass
class BeatmapCollection:
    ID: int
    name: str
    description: str
    # don’t care about the uploader, dateUploaded, dateLastModified, favourites, comments, unsubmittedBeatmapCount, unknownChecksums
    beatmapcount: int
    osucount: int # don’t care about the other modes
    beatmapsets: list[APIBeatmapSet]
    difficultyspread: dict[int, int]
    bpmspread : dict[int, int]

@dataclass
class FallbackResult:
    """No metadata available, but there's a URL worth trying."""
    beatmapset_id: int
    download_url: str
    reason: str  # the original "error" string, kept for logging/debugging

class MirrorAPIError(Exception):
    """Raised when the mirror explicitly reports failure."""
    def __init__(self, message: str, beatmapset_id: int, fallback_url: Optional[str] = None):
        self.message = message
        self.beatmapset_id = beatmapset_id
        super().__init__(message)

@dataclass
class MirrorAPIResponse:
    success: bool
    error: str | None
    beatmapset_id: int
    fallback_url: str | None
    cascade_ms: int
    metadata: dict | None # needs to be parsed first into a BeatmapSet if success is true

def verify_download_url(url: str, timeout: float = 5.0) -> bool:
    """Confirm the URL actually serves a file before trusting it."""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        if resp.status_code != 200:
            return False
        content_type = resp.headers.get("Content-Type", "")
        # osz mirrors sometimes lie with octet-stream, sometimes correct mime — check both angles
        if "application/json" in content_type:
            return False  # this is an error page, not a file
        content_length = resp.headers.get("Content-Length")
        if content_length is not None and int(content_length) < 1024:
            return False  # suspiciously small — probably an error body, not an osz
        return True
    except requests.RequestException:
        return False

def fetch_beatmapset(id: int, want_metadata:bool = True, depth: int = 0) -> BeatmapSet | FallbackResult | tuple[bytes, Iterator[bytes]]:
    url = f"{MIRROR_API}{id}"
    params = {"include": "all", "format": "json"} if want_metadata else {"noVideo": 1}
    response = requests.get(url, params=params, stream=not want_metadata)
    if response.status_code == 404:
        raise MirrorAPIError(
            message="Beatmapset not found on any mirror",
            beatmapset_id=id
        )
    if response.status_code == 429:
        # check if we have retried too many times
        if depth >= 5:
            raise MirrorAPIError(
                message="Rate limited too many times when fetching beatmapset",
                beatmapset_id=id
            )
        # we are rate limited, wait 5s
        time.sleep(5)
        logging.warning(f"Rate limited when fetching beatmapset {id}, retrying after 5 seconds.")
        return fetch_beatmapset(id, want_metadata, depth + 1)  # retry

    response.raise_for_status()


    # the following code will raise exceptions for each step of checking the response
    
    content_type = response.headers.get('Content-Type', '')

    if 'application/json' in content_type:
        data = response.json()
        if data.get('success'):
            metadata = data.get('metadata', {})
            return BeatmapSet(
                success=data['success'],
                download_url=data['download_url'],
                beatmapset_id=data['beatmapset_id'],
                source=data['source'],
                cascade_ms=data['cascade_ms'],
                title=metadata.get('title', ''),
                artist=metadata.get('artist', ''),
                creator=metadata.get('creator', ''),
                status=metadata.get('status', ''),
                bpm=metadata.get('bpm', 0.0),
                beatmaps=[
                    Beatmap(
                        id=bm['id'],
                        version=bm['version'],
                        difficulty_rating=bm['difficulty_rating'],
                        pp_data=PPDataSet.from_dict(bm.get('pp_data', {}))
                    ) for bm in metadata.get('beatmaps', [])
                ],
            )

        if data.get('fallback_url'):
            # check for a fallback URL in the response and verify it before returning
            url = data['fallback_url']
            valid = verify_download_url(url)
            if valid:
                return FallbackResult(
                    beatmapset_id=data['beatmapset_id'],
                    download_url=data['fallback_url'],
                    reason=data.get('error', 'Unknown error')
                )
            raise MirrorAPIError(
                message=f"Fallback URL is invalid: {data.get('error', 'Unknown error')}",
                beatmapset_id=data['beatmapset_id'],
                fallback_url=data['fallback_url']
            )

        raise MirrorAPIError(
            message=data.get('error', 'Unknown error'),
            beatmapset_id=data['beatmapset_id'],
        )

    # Check if the response is a valid .osz file by looking at the content type or content disposition
    chunk_iter: Iterator[bytes] = response.iter_content(chunk_size=8192)
    
    check_result = check_for_osz(chunk_iter)

    if check_result:
        return check_result  # returns (first_chunk, chunk_iter) if valid .osz

    raise MirrorAPIError(
        message='Response is not JSON and does not appear to be a valid .osz file',
        beatmapset_id=id
    )

def fetch_collection(id: int) -> BeatmapCollection:
    url = f"{COLLECTION_API}{id}"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    return BeatmapCollection(
        ID=data['id'],
        name=data['name'],
        description=data['description'],
        beatmapcount=data['beatmapCount'],
        osucount=data['modes']['osu'],
        beatmapsets=[
            APIBeatmapSet(
                id=bms['id'],
                beatmaps=bms['beatmaps']
            ) for bms in data.get('beatmapsets', [])
        ],
        difficultyspread=data.get('difficultyspread', {}),
        bpmspread=data.get('bpmspread', {})
    )

def download_beatmapset(beatmapset: BeatmapSet, output_dir: Path) -> bool:
    """Download the .osz file for a BeatmapSet and save it to the specified directory. Returns True if successful, False otherwise."""
    os.makedirs(output_dir, exist_ok=True)
    filename = create_file_name(beatmapset.artist, beatmapset.title, beatmapset.beatmapset_id)
    # Check if the file already exists
    if (output_dir / filename).exists():
        logging.info(f"Beatmapset {beatmapset.beatmapset_id} already downloaded as {filename}.")
        return True, 0 # Already downloaded

    # Check fallback name just in case
    if (output_dir / f"{beatmapset.beatmapset_id}.osz").exists():
        logging.info(f"Beatmapset {beatmapset.beatmapset_id} already downloaded as {beatmapset.beatmapset_id}.osz.")
        return True, 0 # Already downloaded

    try:
        first_chunk, chunk_iter = fetch_beatmapset(beatmapset.beatmapset_id, want_metadata=False)
    except MirrorAPIError as e:
        logging.error(f"Failed to fetch beatmapset {beatmapset.beatmapset_id}: {e.message}")
        return False, 0
    
    total_bytes = len(first_chunk)
    with open(output_dir / filename, 'wb') as f:
        f.write(first_chunk)
        for chunk in chunk_iter:
            f.write(chunk)
            total_bytes += len(chunk)
    return True, total_bytes

def download_beatmapset_from_fallback(fallback: FallbackResult, output_dir: Path) -> bool:
    """Download the .osz file for a BeatmapSet from a fallback URL and save it to the specified directory. Returns True if successful, False otherwise."""
    os.makedirs(output_dir, exist_ok=True)
    name = f"{fallback.beatmapset_id}.osz"

    # Check if the file already exists
    if (output_dir / name).exists():
        logging.info(f"Beatmapset {fallback.beatmapset_id} already downloaded as {name}.")
        return True, 0 # Already downloaded

    try:
        response = requests.get(fallback.download_url, stream=True)
        response.raise_for_status()
        check = check_for_osz(response.iter_content(chunk_size=8192))
        if not check:
            return False, 0  # Not a valid .osz file

        total_bytes = len(check[0])
        with open(output_dir / name, 'wb') as f:
            f.write(check[0])  # Write the first chunk
            for chunk in check[1]:  # Write the rest of the chunks
                f.write(chunk)
                total_bytes += len(chunk)
        return True, total_bytes
    except Exception as e:
        logging.error(f"Failed to download beatmapset {fallback.beatmapset_id} from fallback URL: {e}")
        return False, 0

@dataclass
class DownloadProgress:
    total: int
    completed: int = 0
    current: Optional[str] = None
    finished: bool = False
    bytes_downloaded: int = 0  # Total bytes downloaded so far

def download_collection(
    collection_id: int,
    output_dir: Path,
    requested: int,
    start: int = 0,
    osu_filter: OsuFilter | None = None,
) -> Iterator[DownloadProgress]:
    '''Downloads a collection of beatmapsets, yielding progress updates.'''

    collection = fetch_collection(collection_id)
    logging.info(
        f"Starting download of collection '{collection.name}' "
        f"with {collection.osucount} beatmapsets."
    )

    osu_filter = osu_filter or OsuFilter()

    downloaded = 0
    processed = 0
    bytes_total = 0  # Total bytes downloaded so far

    # This is the denominator shown on the progress bar.
    current_total = requested

    beatmapsets = collection.beatmapsets[start:]

    for api_beatmapset in beatmapsets:

        current = (
            f"Downloading beatmapset {api_beatmapset.id} "
            f"({downloaded + 1}/{current_total})"
        )

        try:
            result = fetch_beatmapset(api_beatmapset.id)

            if isinstance(result, BeatmapSet):

                if result.filter(osu_filter):
                    success, nbytes = download_beatmapset(result, output_dir)

                    if success:
                        downloaded += 1
                        bytes_total += nbytes
                        logging.info(
                            f"Downloaded {result.artist} - {result.title}"
                        )
                    else:
                        logging.error(
                            f"Failed downloading {result.beatmapset_id}"
                        )
                else:
                    logging.info(
                        f"Skipping {result.artist} - {result.title} due to filter."
                    )

            elif isinstance(result, FallbackResult):
                # First, check if we are using a mod filter. If we are, we should skip this beatmapset because we don't have metadata to check against.
                if osu_filter.mods:
                    logging.warning(
                        f"Skipping {result.beatmapset_id} due to mod filter; no metadata available."
                        )
                
                else:
                    success, nbytes = download_beatmapset_from_fallback(result, output_dir)

                    if success:
                        downloaded += 1
                        bytes_total += nbytes
                        logging.info(
                            f"Downloaded {result.beatmapset_id} using fallback."
                        )
                    else:
                        logging.error(
                            f"Fallback download failed for {result.beatmapset_id}"
                        )

        except MirrorAPIError as e:
            logging.error(f"Error fetching {api_beatmapset.id}: {e.message}")

        # We've now completely finished processing one beatmap.
        processed += 1

        remaining = len(beatmapsets) - processed

        # Best-case number of downloads still achievable.
        current_total = min(requested, downloaded + remaining)

        finished = (
            downloaded >= requested
            or downloaded >= current_total
        )

        yield DownloadProgress(
            total=current_total,
            completed=downloaded,
            current=current,
            finished=finished,
            bytes_downloaded=bytes_total
        )

        if finished:
            break


if __name__ == "__main__":
    # Gather user settings
    collection_id, limit, start, min_stars, max_stars, min_bpm, max_bpm, mods, collection = gather_settings()

    Filter = OsuFilter(minstar=min_stars, maxstar=max_stars, minbpm=min_bpm, maxbpm=max_bpm, mods=mods)

    # Download the collection with progress reporting
    tqdm_bar = tqdm(total=limit, desc="Downloading Beatmapsets", unit="map")

    last_time = time.time()
    last_bytes = 0

    for progress in download_collection(collection_id, Path("osz_downloads"), requested=limit, start=start, osu_filter=Filter):
        now = time.time()
        elapsed = now - last_time
        delta_bytes = progress.bytes_downloaded - last_bytes

        speed_bps = delta_bytes / elapsed if elapsed > 0 else 0
        speed_str = f"{speed_bps / 1024 / 1024:.2f} MB/s" if speed_bps > 1024 * 1024 else f"{speed_bps / 1024:.1f} KB/s"

        last_time = now
        last_bytes = progress.bytes_downloaded

        tqdm_bar.n = progress.completed
        tqdm_bar.set_postfix_str(f"{speed_str}")
        tqdm_bar.refresh()
        if progress.finished:
            break

    # Open the downloaded osz files via a batch file if the user confirms
    confirm = input("\nDo you want to open the downloaded .osz files? (y/n): ")
    if confirm.lower() == "y":
        # run the batch file to open the .osz files (the command is this: powershell -Command "Get-ChildItem '.\osz_downloads\*.osz' | ForEach-Object { Start-Process $_.FullName; Start-Sleep -Milliseconds 1000 }")
        subprocess.run('load_maps.bat', shell=True)