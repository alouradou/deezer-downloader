import time
import os.path
from os.path import basename
import mpd
import platform
from zipfile import ZipFile, ZIP_DEFLATED

from deezer_downloader.configuration import config
from deezer_downloader.youtubedl import youtubedl_download
from deezer_downloader.spotify import get_songs_from_spotify_website
from deezer_downloader.deezer import TYPE_TRACK, TYPE_ALBUM, TYPE_PLAYLIST, get_song_infos_from_deezer_website, \
    download_song, parse_deezer_playlist, deezer_search, get_deezer_favorites, download_deezer_playlist_informations, \
    download_deezer_favorite_informations, load_deezer_json_informations, get_deezer_user_playlists_json
from deezer_downloader.deezer import Deezer403Exception, Deezer404Exception

from deezer_downloader.threadpool_queue import ThreadpoolScheduler, report_progress
sched = ThreadpoolScheduler()


def check_download_dirs_exist():
    for directory in [config["download_dirs"]["songs"], config["download_dirs"]["zips"], config["download_dirs"]["albums"],
                      config["download_dirs"]["playlists"], config["download_dirs"]["youtubedl"]]:
        os.makedirs(directory, exist_ok=True)


check_download_dirs_exist()


def make_song_paths_relative_to_mpd_root(songs, prefix=""):
    # ensure last slash
    config["mpd"]["music_dir_root"] = os.path.join(config["mpd"]["music_dir_root"], '')
    songs_paths_relative_to_mpd_root = []
    for song in songs:
        songs_paths_relative_to_mpd_root.append(prefix + song[len(config["mpd"]["music_dir_root"]):])
    return songs_paths_relative_to_mpd_root


def update_mpd_db(songs, add_to_playlist):
    # songs: list of music files or just a string (file path)
    if not config["mpd"].getboolean("use_mpd"):
        return
    print("Updating mpd database")
    timeout_counter = 0
    mpd_client = mpd.MPDClient(use_unicode=True)
    try:
        mpd_client.connect(config["mpd"]["host"], config["mpd"].getint("port"))
    except ConnectionRefusedError as e:
        print("ERROR connecting to MPD ({}:{}): {}".format(config["mpd"]["host"], config["mpd"]["port"], e))
        return
    mpd_client.update()
    if add_to_playlist:
        songs = [songs] if type(songs) != list else songs
        songs = make_song_paths_relative_to_mpd_root(songs)
        while len(mpd_client.search("file", songs[0])) == 0:
            # c.update() does not block so wait for it
            if timeout_counter == 10:
                print("Tried it {} times. Give up now.".format(timeout_counter))
                return
            print("'{}' not found in the music db. Let's wait for it".format(songs[0]))
            timeout_counter += 1
            time.sleep(2)
        for song in songs:
            try:
                mpd_client.add(song)
                print("Added to mpd playlist: '{}'".format(song))
            except mpd.base.CommandError as mpd_error:
                print("ERROR adding '{}' to playlist: {}".format(song, mpd_error))


def clean_filename(path):
    path = path.replace("\t", " ")
    if any(platform.win32_ver()):
        path.replace("\"", "'")
        array_of_special_characters = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
    else:
        array_of_special_characters = ['/', ':', '"', '?']

    return ''.join([c for c in path if c not in array_of_special_characters])


def create_hardlink_from_itunes(source_path, dest_path):
    """Retrieve an audio file from Music Library via hardlink"""
    try:
        print(f"Creating link: {source_path} → {dest_path}")
        os.link(source_path, dest_path)
    except FileExistsError:
        print(f"File {os.path.basename(dest_path)} already exists in the current directory!")
    except Exception as e:
        print(f"Error while creating link for {file_name}: {e}. Ignoring.")


def download_song_and_get_absolute_filename(search_type, song, playlist_name=None):
    if config["download_dirs"]["itunes_library"]:
        itunes_path = config["download_dirs"]["itunes_library"]
        itunes_library = itunes_path + "/Media.localized/Automatically Add to Music.localized"
        media_music_path = itunes_path + "/Media.localized/Music"
    else:
        itunes_library = None
        media_music_path = None

    if search_type == TYPE_ALBUM:
        song_filename = "{:02d} - {} {}.mp3".format(int(song['TRACK_NUMBER']),
                                                    song['ART_NAME'],
                                                    song['SNG_TITLE'])
    else:
        song_filename = "{} - {}.mp3".format(song['ART_NAME'],
                                             song['SNG_TITLE'])
    song_filename = clean_filename(song_filename)

    absolute_filename = ""

    if search_type == TYPE_TRACK:
        absolute_filename = os.path.join(config["download_dirs"]["songs"], song_filename)
    elif search_type == TYPE_ALBUM:
        album_name = "{} - {}".format(song['ART_NAME'], song['ALB_TITLE'])
        album_name = clean_filename(album_name)
        album_dir = os.path.join(config["download_dirs"]["albums"], album_name)
        if not os.path.exists(album_dir):
            os.mkdir(album_dir)
        absolute_filename = os.path.join(album_dir, song_filename)
    elif search_type == TYPE_PLAYLIST:
        assert type(playlist_name) == str
        playlist_name = clean_filename(playlist_name)
        playlist_dir = os.path.join(config["download_dirs"]["playlists"], playlist_name)
        if not os.path.exists(playlist_dir):
            os.mkdir(playlist_dir)
        absolute_filename = os.path.join(playlist_dir, song_filename)

    itunes_song_path = None
    if media_music_path:
        artist_folder = clean_filename(song['ART_NAME'])
        album_folder = clean_filename(song['ALB_TITLE'])
        track_number = "{:02d}".format(int(song.get('TRACK_NUMBER', 0)))
        disk_number = song.get('DISK_NUMBER', 1)
        music_filename = f"{disk_number}-{track_number} {song['SNG_TITLE']}.mp3"
        itunes_song_path = os.path.join(media_music_path, artist_folder, album_folder, music_filename)

    if itunes_song_path and os.path.exists(itunes_song_path):
        print(f"Skipping song '{itunes_song_path}'. Already exists in Music App.")
        create_hardlink_from_itunes(itunes_song_path, absolute_filename)
        return absolute_filename
    elif os.path.exists(absolute_filename):
        print("Skipping song '{}'. Already exists.".format(absolute_filename))
        return absolute_filename
    elif os.path.exists(absolute_filename.replace('.mp3', '.flac')):
        print("Skipping song '{}'. Already exists.".format(absolute_filename.replace('.mp3', '.flac')))
        return absolute_filename

    print("Downloading '{}'".format(song_filename))
    download_song(song, absolute_filename, itunes_library)
    print("Downloaded '{}'".format(absolute_filename))

    return absolute_filename


def download_informations_and_get_absolute_filename(search_type, playlist_id="", playlist_name=None):
    if search_type == TYPE_TRACK:
        absolute_filename = config["download_dirs"]["songs"]
    elif search_type == TYPE_ALBUM:
        album_dir = config["download_dirs"]["albums"]
        if not os.path.exists(album_dir):
            os.mkdir(album_dir)
        absolute_filename = os.path.join(album_dir, "album_informations.json")
    elif search_type == TYPE_PLAYLIST:
        playlist_name = clean_filename(playlist_name)
        playlist_dir = os.path.join(config["download_dirs"]["playlists"], playlist_name)
        if not os.path.exists(playlist_dir):
            os.mkdir(playlist_dir)
        absolute_filename = os.path.join(playlist_dir, "playlist_informations.json")

        if os.path.exists(absolute_filename):
            print("New informations found about '{}'. Updating.".format(absolute_filename))
        else:
            print("Downloading informations about '{}'".format(absolute_filename))
        download_deezer_playlist_informations(playlist_id, absolute_filename)
    return absolute_filename


def compare_existing_favorite_json_file(user_id, absolute_filename):
    playlist_dir = os.path.dirname(absolute_filename)
    if not os.path.exists(absolute_filename):
        print(f"The file {absolute_filename} doesn't exist. Getting informations.")
        if not os.path.exists(playlist_dir):
            os.mkdir(playlist_dir)
        resp_playlist_file = download_deezer_favorite_informations(user_id, absolute_filename)
        data_to_download = [item for item in resp_playlist_file.get("data", [])]
    else:
        # Load the old file at absolute_filename path and put it in a dictionary
        prev_playlist_file = load_deezer_json_informations(absolute_filename)
        prev_ids = {item['id'] for item in prev_playlist_file.get("data", [])}

        # Download the new information about the playlist
        resp_playlist_file = download_deezer_favorite_informations(user_id, absolute_filename)
        resp_ids = {item['id'] for item in resp_playlist_file.get("data", [])}

        data_to_download = [
            item for item in resp_playlist_file.get("data", [])
            if item["id"] in (resp_ids - prev_ids)
        ]

    print(f"Favorite playlist differ with {len(data_to_download)} new songs.")

    resp_json = {
        "data": data_to_download,
        "total": len(data_to_download)
    }
    return [song['id'] for song in resp_json['data']]


def compare_existing_playlist_json_file(playlist_id, absolute_filename):
    playlist_dir = os.path.dirname(absolute_filename)
    if not os.path.exists(absolute_filename):
        print(f"The file {absolute_filename} doesn't exist. Getting informations.")
        if not os.path.exists(playlist_dir):
            os.mkdir(playlist_dir)
        resp_playlist_file = download_deezer_playlist_informations(playlist_id, absolute_filename)
        data_to_download = [item for item in resp_playlist_file.get("SONGS", {}).get("data", [])]
    else:
        # Load the old file at absolute_filename path and put it in a dictionary
        prev_playlist_file = load_deezer_json_informations(absolute_filename)
        prev_ids = {item['SNG_ID'] for item in prev_playlist_file.get("results", {})
                                                                 .get("SONGS", {}).get("data", [])}

        # Download the new information about the playlist
        resp_playlist_file = download_deezer_playlist_informations(playlist_id, absolute_filename)
        resp_ids = {item['SNG_ID'] for item in resp_playlist_file.get("SONGS", {}).get("data", [])}

        data_to_download = [
            item for item in resp_playlist_file.get("SONGS", {}).get("data", [])
            if item["SNG_ID"] in (resp_ids - prev_ids)
        ]

    print(f"Playlist differ with {len(data_to_download)} new songs.")

    resp_json = {
        "data": data_to_download,
        "total": len(data_to_download)
    }
    return [song for song in resp_json['data']]


def create_zip_file(songs_absolute_location):
    # take first song in list and take the parent dir (name of album/playlist")
    parent_dir = basename(os.path.dirname(songs_absolute_location[0]))
    location_zip_file = os.path.join(config["download_dirs"]["zips"], "{}.zip".format(parent_dir))
    print("Creating zip file '{}'".format(location_zip_file))
    with ZipFile(location_zip_file, 'w', compression=ZIP_DEFLATED) as zip:
        for song_location in songs_absolute_location:
            try:
                print("Adding song {}".format(song_location))
                zip.write(song_location, arcname=os.path.join(parent_dir, basename(song_location)))
            except FileNotFoundError:
                print("Could not find file '{}'".format(song_location))
    print("Done with the zip")
    return location_zip_file


def create_m3u8_file(songs_absolute_location):
    playlist_directory, __ = os.path.split(songs_absolute_location[0])
    # 00 as prefix => will be shown as first in dir listing
    m3u8_filename = "{}.m3u8".format(os.path.basename(playlist_directory))
    print("Creating m3u8 file: '{}'".format(m3u8_filename))
    m3u8_file_abs = os.path.join(playlist_directory, m3u8_filename)
    with open(m3u8_file_abs, "w") as f:
        for song in songs_absolute_location:
            f.write(basename(song) + "\n")
    # add m3u8_file so that will be zipped to
    songs_absolute_location.append(m3u8_file_abs)
    return songs_absolute_location


@sched.register_command()
def get_deezer_user_playlists(user_id: str):
    playlists = get_deezer_user_playlists_json(user_id)
    return playlists


@sched.register_command()
def download_deezer_song_and_queue(track_id, add_to_playlist):
    song = get_song_infos_from_deezer_website(TYPE_TRACK, track_id)
    absolute_filename = download_song_and_get_absolute_filename(TYPE_TRACK, song)
    update_mpd_db(absolute_filename, add_to_playlist)
    return make_song_paths_relative_to_mpd_root([absolute_filename])


@sched.register_command()
def download_deezer_album_and_queue_and_zip(album_id, add_to_playlist, create_zip):
    songs = get_song_infos_from_deezer_website(TYPE_ALBUM, album_id)
    songs_absolute_location = []
    for i, song in enumerate(songs):
        report_progress(i, len(songs))
        assert type(song) == dict
        absolute_filename = download_song_and_get_absolute_filename(TYPE_ALBUM, song)
        songs_absolute_location.append(absolute_filename)
    update_mpd_db(songs_absolute_location, add_to_playlist)
    if create_zip:
        return [create_zip_file(songs_absolute_location)]
    return make_song_paths_relative_to_mpd_root(songs_absolute_location)


@sched.register_command()
def download_deezer_playlist_and_queue_and_zip(playlist_id, add_to_playlist, create_zip,
                                               informations=False, update=False):
    playlist_name, songs = parse_deezer_playlist(playlist_id)
    if informations:
        absolute_filename = os.path.join(config["download_dirs"]["playlists"],
                                         playlist_name, "playlist_informations.json")
        if not add_to_playlist and not create_zip:
            return download_informations_and_get_absolute_filename(TYPE_PLAYLIST, playlist_id, playlist_name)
        elif update and add_to_playlist:
            songs = compare_existing_playlist_json_file(playlist_id, absolute_filename)
            if not songs:
                return
    songs_absolute_location = []
    for i, song in enumerate(songs):
        report_progress(i, len(songs))
        absolute_filename = download_song_and_get_absolute_filename(TYPE_PLAYLIST, song, playlist_name)
        songs_absolute_location.append(absolute_filename)
    update_mpd_db(songs_absolute_location, add_to_playlist)
    songs_with_m3u8_file = create_m3u8_file(songs_absolute_location)
    if create_zip:
        return [create_zip_file(songs_with_m3u8_file)]
    return make_song_paths_relative_to_mpd_root(songs_absolute_location)


@sched.register_command()
def download_spotify_playlist_and_queue_and_zip(playlist_name, playlist_id, add_to_playlist, create_zip):
    songs = get_songs_from_spotify_website(playlist_id,
                                           config["proxy"]["server"])
    songs_absolute_location = []
    print(f"We got {len(songs)} songs from the Spotify playlist")
    for i, song_of_playlist in enumerate(songs):
        report_progress(i, len(songs))
        # song_of_playlist: string (artist - song)
        try:
            track_id = deezer_search(song_of_playlist, TYPE_TRACK)[0]['id'] #[0] can throw IndexError
            song = get_song_infos_from_deezer_website(TYPE_TRACK, track_id)
            absolute_filename = download_song_and_get_absolute_filename(TYPE_PLAYLIST, song, playlist_name)
            songs_absolute_location.append(absolute_filename)
        except (IndexError, Deezer403Exception, Deezer404Exception) as msg:
            print(msg)
            print(f"Could not find Spotify song ({song_of_playlist}) on Deezer?")
            # return
    update_mpd_db(songs_absolute_location, add_to_playlist)
    songs_with_m3u8_file = create_m3u8_file(songs_absolute_location)
    if create_zip:
        return [create_zip_file(songs_with_m3u8_file)]
    return make_song_paths_relative_to_mpd_root(songs_absolute_location)


@sched.register_command()
def download_youtubedl_and_queue(video_url, add_to_playlist):
    filename_absolute = youtubedl_download(video_url,
                                           config["download_dirs"]["youtubedl"],
                                           config["proxy"]["server"])
    update_mpd_db(filename_absolute, add_to_playlist)
    return make_song_paths_relative_to_mpd_root([filename_absolute])


@sched.register_command()
def download_deezer_favorites(user_id: str, add_to_playlist: bool, create_zip: bool,
                              informations: bool = False, update: bool = False):
    songs_absolute_location = []
    output_directory = f"favorites_{user_id}"
    favorite_songs = get_deezer_favorites(user_id)
    if informations:
        absolute_filename = os.path.join(config["download_dirs"]["playlists"],
                                         output_directory, "playlist_informations.json")
        if not add_to_playlist and not create_zip:
            return download_deezer_favorite_informations(user_id, absolute_filename)
        elif update and add_to_playlist:
            favorite_songs = compare_existing_favorite_json_file(user_id, absolute_filename)
            if not favorite_songs:
                return
    for i, fav_song in enumerate(favorite_songs):
        report_progress(i, len(favorite_songs))
        try:
            song = get_song_infos_from_deezer_website(TYPE_TRACK, fav_song)
            absolute_filename = download_song_and_get_absolute_filename(TYPE_PLAYLIST, song, output_directory)
            songs_absolute_location.append(absolute_filename)
        except (IndexError, Deezer403Exception, Deezer404Exception) as msg:
            print(msg)
            print(f"Could not find song ({fav_song}) on Deezer?")
    update_mpd_db(songs_absolute_location, add_to_playlist)
    songs_with_m3u8_file = create_m3u8_file(songs_absolute_location)
    if create_zip:
        return [create_zip_file(songs_with_m3u8_file)]
    return make_song_paths_relative_to_mpd_root(songs_absolute_location)


if __name__ == '__main__':
    pass
    #download_spotify_playlist_and_queue_and_zip("test", '21wZXvtrERELL0bVtKtuUh', False, False)
