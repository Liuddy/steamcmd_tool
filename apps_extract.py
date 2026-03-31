import concurrent.futures
import csv
import os
import re
import subprocess
import sys
import time
from dotenv import load_dotenv
import requests
import vdf

# --- Preliminary steps ---
# Connect to Steam and extract every licenses you own with the following command line
# (replace "username" with yours and change steamcmd.exe path if needed):
# C:\steamcmd\steamcmd.exe +login username +licenses_print +quit > licenses.txt

load_dotenv(override=True)

CMD_LINE_BATCH = 500            # To avoid Windows command line maximum char limit
CMD_LINE_DELAY = 60             # To wait for SteamCMD output
API_CALLS_DELAY = 1             # To avoid Steam API calls rate-limit
MAX_THREADS = 5                 # To limit number of workers on multi-thread
SESSION = requests.Session()    # To not open a new session for each API call

LICENSES_FILE_PATH = os.environ['LICENSES_FILE_PATH']
ALL_GAMES_CSV_FILE_PATH = os.environ['ALL_GAMES_CSV_FILE_PATH']
PROFILE_GAMES_CSV_FILE_PATH = os.environ['PROFILE_GAMES_CSV_FILE_PATH']
GAMES_DLC_CSV_FILE_PATH = os.environ['GAMES_DLC_CSV_FILE_PATH']
MISC_CSV_FILE_PATH = os.environ['MISC_CSV_FILE_PATH']
FAMILY_CSV_FILE_PATH = os.environ['FAMILY_CSV_FILE_PATH']

STEAM_CMD_EXE = os.environ['STEAM_CMD_EXE']
STEAM_KEY = os.environ['STEAM_KEY']
STEAM_ID = os.environ['STEAM_ID']
STEAM_API_PROFILE_URL = f'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key={STEAM_KEY}&steamid={STEAM_ID}&include_appinfo=true&include_played_free_games=true&skip_unvetted_apps=false&format=json'

# --- Logging configuration ---
LOG_FILE_PATH = 'logs.txt'
BACKLOGS_FILE_PATH = 'backlogs.txt'

class Logger:
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, 'w', encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        pass    # Trigger immediate writing

sys.stdout = Logger(LOG_FILE_PATH)

# --- Rename logs.txt to backlogs.txt ---
def stop_logging():
    try:
        sys.stdout.log.close()
        if os.path.exists(BACKLOGS_FILE_PATH):
            os.remove(BACKLOGS_FILE_PATH)
        os.rename(LOG_FILE_PATH, BACKLOGS_FILE_PATH)
        sys.__stdout__.write(f'\033[92m\n[INFO] Logs file saved to {BACKLOGS_FILE_PATH}.\033[0m')
    except Exception as e:
        sys.__stdout__.write(f'\033[91m\n[ERR] Failed to save logs file: {e}\033[0m')

# Allow to print colors in terminal
if os.name == 'nt':
    os.system('')


# --- App class defining each Steam app ---
class App:
    def __init__(self, app_id, name = None, app_type = None, price = None, on_store = None, on_profile = None, owned = None, dlc_list = None):
        self.id = app_id
        self.name = name
        self.type = app_type                # Indicate if it's a game, an app, a DLC, a music, a tool or else.
        self.price = price                  # Indicate if it's a free / paid app or coming from family sharing.
        self.on_store = on_store            # Indicate if it's available on store or not.
        self.on_profile = on_profile        # Indicate if it's listed on Steam profile games or not.
        self.owned = owned
        self.dlc_list = dlc_list

    def __eq__(self, app):
        return (
            self.id == app.get_id()
            and self.name == app.get_name()
            and self.type == app.get_type()
            and self.price == app.get_price()
            and self.on_store == app.is_on_store()
            and self.on_profile == app.is_on_profile()
            and self.owned == app.is_owned()
            and self.dlc_list == app.get_dlc_list()
        )

    def get_id(self):
        return self.id

    def get_name(self):
        return self.name
    def set_name(self, name):
        self.name = name

    def get_type(self):
        return self.type
    def set_type(self, app_type):
        self.type = app_type

    def get_price(self):
        return self.price
    def set_price(self, price):
        self.price = price

    def is_on_store(self):
        return self.on_store
    def set_on_store(self, on_store):
        self.on_store = on_store

    def is_on_profile(self):
        return self.on_profile
    def set_on_profile(self, on_profile):
        self.on_profile = on_profile

    def is_owned(self):
        return self.owned
    def set_owned(self, owned):
        self.owned = owned

    def get_dlc_list(self):
        return self.dlc_list
    def set_dlc_list(self, dlc_list):
        self.dlc_list = dlc_list
    def add_dlc(self, dlc):
        self.dlc_list.append(dlc)


# --- Read licenses.txt and create an App for each appID found ---
def make_app_list():
    apps = []
    saved_apps = {}
    with open(LICENSES_FILE_PATH, encoding='utf-8') as f:
        app_price = None
        for line in f:
            if re.search(r'\bState\b\s*:', line):
                app_price = find_app_price(line)
                continue
            if re.search(r'\bApps\b\s*:', line):
                find_app_id(line, app_price, saved_apps, apps)
    print(f'\033[92m\n[INFO] licenses.txt read: {len(apps)} apps found.\033[0m')
    return apps

# --- Find the app price from provided line ---
def find_app_price(line):
    app_price = 'Paid'
    if re.search(r'\bFamily Group\b', line):
        app_price = 'Family'
    elif re.search(r'\bComplimentary\b', line):
        app_price = 'Free'
    return app_price

# --- Find the app ID from provided line ---
def find_app_id(line, app_price, saved_apps, apps):
    app_ids = re.findall(r'(\d+)(?=,)', line)
    for app_id in app_ids:
        if app_id in saved_apps:
            if saved_apps[app_id] != app_price and app_price != 'Family':
                saved_apps[app_id] = app_price
                replace_app_price(apps, app_id, app_price)
            continue
        else:
            saved_apps[app_id] = app_price
            apps.append(App(app_id, price = app_price))
        print(f'\n[DEBUG] Read app {app_id}, price: {app_price}')

# --- Find the app from app ID and replace its price status ---
def replace_app_price(apps, app_id, app_price):
    for app in apps:
        if app.get_id() == app_id:
            app.set_price(app_price)


# --- Complete the informations of each app by reading previous logs file ---
def complete_apps_from_backlogs(apps):
    with open(BACKLOGS_FILE_PATH, encoding='utf-8') as f:
        for line in f:
            if '[DEBUG]' not in line:
                continue
            match_name_type = re.search(r'Completed app (\d+), name:\s*(.+?)\s\|\stype:\s*([^\r\n]+)', line)
            match_status = re.search(r'Completed app (\d+), on store:\s*(.+?)\s\|\son profile:\s*([^\r\n]+)', line)
            if not match_name_type and not match_status:
                continue
            for app in apps:
                if match_name_type and match_name_type.group(1) == app.get_id():
                    app.set_name(match_name_type.group(2))
                    app.set_type(match_name_type.group(3))
                    print(f'\n[DEBUG] Completed app {app.get_id()}, name: {app.get_name()} | type: {app.get_type()}')
                    break
                if match_status and match_status.group(1) == app.get_id():
                    app.set_on_store(match_status.group(2) == 'True')
                    app.set_on_profile(match_status.group(3) == 'True')
                    print(f'\n[DEBUG] Completed app {app.get_id()}, on store: {app.is_on_store()} | on profile: {app.is_on_profile()}')
                    break
    print('\033[92m\n[INFO] Completed apps info from backlogs.txt.\033[0m')


# --- Complete the name and type of each app using SteamCMD ---
def complete_apps_name_and_type(apps):
    apps_to_do = [app for app in apps if not app.get_name() or not app.get_type()]
    vdf_parsed = get_parsed_output(apps_to_do)
    for app in apps_to_do:
        app_data = vdf_parsed.get(app.get_id()).get(app.get_id())   # Doubled because VDF returns the block with app ID
        if not app_data:
            app.set_name('[ERROR]')
            app.set_type('[ERROR]')
        else:
            app_data = app_data.get('common', {})
            if not app_data:
                app.set_name('[UNKNOWN]')
                app.set_type('Sub / Service')
            else:
                app.set_name(app_data.get('name', '[ERROR]'))
                app.set_type(app_data.get('type', '[ERROR]'))
        print(f'\n[DEBUG] Completed app {app.get_id()}, name: {app.get_name()} | type: {app.get_type()}')
    print('\033[92m\n[INFO] Completed apps name and type.\033[0m')

# --- Get and parse CMD output batch per batch for performance ---
def get_parsed_output(apps):
    vdf_parsed = {}
    for i in range(0, len(apps), CMD_LINE_BATCH):
        batch_apps = apps[i:i+CMD_LINE_BATCH]
        cmd_line = make_cmd_line(batch_apps)
        if not cmd_line:
            print(f'\033[93m\n[WARN] Steam CMD line making failed with i = {i}\033[0m')
            continue
        cmd_output = execute_cmd_line(cmd_line)
        if not cmd_output:
            print(f'\033[93m\n[WARN] Steam CMD output failed with i = {i}\033[0m')
            continue
        vdf_batch = parse_output_to_vdf(cmd_output)
        if not vdf_batch:
            print(f'\033[93m\n[WARN] VDF parsing failed with i = {i}\033[0m')
            continue
        vdf_parsed.update(vdf_batch)
    return vdf_parsed

# --- Make one command line in order to limit subprocesses ---
def make_cmd_line(apps):
    cmd_line = [STEAM_CMD_EXE, '+login', 'anonymous']
    for app in apps:
        cmd_line += ['+app_info_print', str(app.get_id())]
    cmd_line += ['+quit']
    print('\033[92m\n[INFO] CMD line made.\033[0m')
    return cmd_line

# --- Execute the command line and return the output ---
def execute_cmd_line(cmd_line):
    print('\n[INFO] CMD line executing...')
    try:
        result = subprocess.run(
            cmd_line,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=CMD_LINE_DELAY,
            check=False
        )
        output = result.stdout
        if not output:
            return None
        print('\033[92m\n[INFO] CMD line executed.\033[0m')
        return output
    except Exception as e:
        print(f'\033[91m\n[ERR] SteamCMD subprocess failed: {e}\033[0m')
        return None

# --- Parse the command line output to get a VDF for each app ID ---
def parse_output_to_vdf(output):
    lines = output.splitlines()
    current_appid = None
    current_block_lines = []
    brace_count = 0
    collecting = False
    parsed_apps = {}
    for line in lines:
        line_strip = line.strip()

        # Get the current app ID
        match_appid = re.match(r'AppID\s*:\s*(\d+),', line_strip)
        if match_appid and not current_appid and not collecting:
            current_appid = match_appid.group(1)
            collecting = False
            continue
        
        # Check if the app ID block is beginning
        if current_appid and re.match(rf'"{current_appid}"', line_strip) and not collecting:
            current_block_lines.append(line)
            collecting = True
            continue
        
        # Collect the block while brace_count > 0
        if collecting:
            if line_strip == '{':
                brace_count += 1
            elif line_strip == '}':
                brace_count -= 1
            current_block_lines.append(line)
            if brace_count == 0 and current_block_lines:
                block_str = '\n'.join(current_block_lines)
                try:
                    parsed_apps[current_appid] = vdf.loads(block_str)
                    print(f'\n[DEBUG] Parsed app {current_appid} to VDF')
                except Exception as e:
                    print(f'\033[91m\n[ERR] VDF parse failed for AppID {current_appid}: {e}\033[0m')
                current_appid = None
                current_block_lines = []
                collecting = False
    print(f'\033[92m\n[INFO] CMD output parsed: {len(list(parsed_apps.keys()))} apps parsed to VDF.\033[0m')
    return parsed_apps


# --- Complete the store and profile status of each app using multi-threaded calls to Steam API ---
def complete_apps_status(apps):
    apps_to_do = [app for app in apps if app.is_on_store() == None or app.is_on_profile() == None]
    check_apps_profile_status(apps_to_do)
    with concurrent.futures.ThreadPoolExecutor(MAX_THREADS) as executor:
        executor.map(check_app_store_status, apps_to_do)
    print('\033[92m\n[INFO] Completed apps status.\033[0m')

# --- Check the profile status of each app based on the API call GetOwnedGames ---
def check_apps_profile_status(apps):
    profile_app_ids = get_profile_app_ids()
    if not profile_app_ids:
        return
    for app in apps:
        if app.get_id() in profile_app_ids:
            app.set_on_profile(True)
        else:
            app.set_on_profile(False)

# --- Return a list of every app IDs from profile using Steam API ---
def get_profile_app_ids():
    app_ids = []
    try:
        r = SESSION.get(STEAM_API_PROFILE_URL, timeout=10)
        data = r.json()
        for app in data.get('response', {}).get('games', []):
            app_id = app.get('appid')
            app_ids.append(str(app_id))
        print(f'\033[92m\n[INFO] Profile apps read: {len(app_ids)} apps found.\033[0m')
        return app_ids
    except Exception as e:
        print(f'\033[91m\n[ERR] Steam API getOwnedGames failed: {e}\033[0m')
        return None

# --- Permits to multi-threaded API calls to check the store status of an app ---
def check_app_store_status(app):
    api_success = get_api_app_details_success(app.get_id())
    if api_success:
        app.set_on_store(True)
    else:
        app.set_on_store(False)
    print(f'\n[DEBUG] Completed app {app.get_id()}, on store: {app.is_on_store()} | on profile: {app.is_on_profile()}')

# --- Get the success status of app_details API call ---
def get_api_app_details_success(app_id):
    url = f'https://store.steampowered.com/api/appdetails?appids={app_id}'
    while True:
        try:
            time.sleep(API_CALLS_DELAY)
            res = SESSION.get(url, timeout=10)
            data = res.json()
            entry = data.get(app_id)
            return entry.get('success', False)
        except Exception:
            time.sleep(API_CALLS_DELAY * 60)


# --- Export every app into categorized CSV files ---
def export_all(apps):
    export_to_CSV(get_all_games(apps), ALL_GAMES_CSV_FILE_PATH)
    export_to_CSV(get_profile_games(apps), PROFILE_GAMES_CSV_FILE_PATH)
    export_to_CSV(get_misc_apps(apps), MISC_CSV_FILE_PATH)
    export_to_CSV(get_family_sharings(apps), FAMILY_CSV_FILE_PATH)

# --- Get only games and apps from app list ---
def get_all_games(apps):
    games = []
    valid_types = ['game', 'demo', 'beta', 'application']
    for app in apps:
        if app.get_type().lower() in valid_types and app.get_price().lower() != 'family':
            games.append(app)
    export_to_CSV(get_games_dlc_apps(games), GAMES_DLC_CSV_FILE_PATH)
    return games

# --- Get only the profile games and apps from app list ---
def get_profile_games(apps):
    profile_games = []
    valid_types = ['game', 'demo', 'beta', 'application']
    for app in apps:
        if app.get_type().lower() in valid_types and app.is_on_profile():
            profile_games.append(app)
    return profile_games

# --- Get only apps other than games and apps from app list ---
def get_misc_apps(apps):
    misc = []
    valid_types = ['game', 'demo', 'beta', 'application']
    for app in apps:
        if app.get_type().lower() not in valid_types and app.get_price().lower() != 'family':
            misc.append(app)
    return misc

# --- Get only the family shared apps from app list ---
def get_family_sharings(apps):
    sharing = []
    for app in apps:
        if app.get_price().lower() == 'family':
            sharing.append(app)
    return sharing

# --- Get only DLCs for owned games from app list ---
def get_games_dlc_apps(games):
    dlc_list = []
    for game in games:
        for dlc in game.get_dlc_list():
            dlc_list.append(dlc)
    return dlc_list

# --- Export the App list to the provided CSV file ---
def export_to_CSV(apps, file_path):
    with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['ID', 'Name', 'Type', 'Price', 'Store', 'Profile', 'Owned'])
        writer.writerows([[
            app.get_id(),
            app.get_name(),
            app.get_type(),
            app.get_price(),
            'Available' if app.is_on_store() else 'Not available',
            'Showing' if app.is_on_profile() else 'Not showing',
            'Owned' if app.is_owned() else 'Not owned'
        ] for app in apps])
    print(f'\033[92m\n[INFO] CSV export done: {file_path}, {len(apps)} apps exported.\033[0m')


def main():
    apps = make_app_list()
    complete_apps_from_backlogs(apps)
    complete_apps_name_and_type(apps)
    complete_apps_status(apps)
    export_all(apps)
    stop_logging()

main()
