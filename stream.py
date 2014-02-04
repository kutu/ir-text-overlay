#!python3

import sys
import shutil
import re
import time
import math
import logging, logging.handlers
import argparse
import json
import irsdk
import twitch

VERSION = '1.0.1'

LICENSE_CLASSES = ['R', 'D', 'C', 'B', 'A', 'P', 'WC']

class State:
    is_connected = False

    last_session_num = -1
    last_session_state = -1
    my_car_idx = -1
    cam_car_idx = -1

    cur_session_time = -1
    cur_session_type = None

    event_type = None
    track_length = -1
    first_sector_pct = -1
    session_laps = -1
    session_time = -1

    rpm_min = -1
    rpm_max = -1
    rpm_len = 10

    drivers = {}

    speed_calc_data = []

    last_time_update_lap_ses_time = -1

    last_time_update_drivers = -1

    last_time_update_positions = -1
    cur_dist_pct = 0
    last_dist_pct = 0

    last_time_update_standing = -1

    twitch = None

class TwitchState:
    channel = None
    oauth_token = None

    status = None
    last_status = None
    twreq_status = None

    last_update = -1
    last_viewers = 0
    last_followers = 0
    last_follower = None
    pending = False
    twreq_stream = None
    twreq_follows = None


def on_session_change():
    if ir['DriverInfo']:
        state.my_car_idx = ir['DriverInfo']['DriverCarIdx']
        state.rpm_min = ir['DriverInfo']['DriverCarSLFirstRPM'] * 2/3
        state.rpm_max = ir['DriverInfo']['DriverCarRedLine']
    else:
        state.my_car_idx = state.rpm_min = state.rpm_max = -1

    if ir['WeekendInfo']:
        state.track_length = float(ir['WeekendInfo']['TrackLength'].split()[0])
    else:
        state.track_length = -1

    if ir['SessionInfo']:
        state.session_laps = ir['SessionInfo']['Sessions'][state.last_session_num]['SessionLaps']
        session_time = ir['SessionInfo']['Sessions'][state.last_session_num]['SessionTime']
        state.session_time = -1 if session_time == 'unlimited' else float(session_time.split()[0])
        state.cur_session_type = ir['SessionInfo']['Sessions'][state.last_session_num]['SessionType']
    else:
        state.session_laps = state.session_time = -1
        state.cur_session_type = None

    if ir['WeekendInfo'] and ir['DriverInfo']:
        state.event_type = ir['WeekendInfo']['EventType']
        if state.twitch:
            track_name = ir['WeekendInfo']['TrackDisplayShortName']
            driver = next(d for d in ir['DriverInfo']['Drivers'] if d['CarIdx'] == state.my_car_idx)
            car_class_name = driver['CarClassShortName'] if driver['CarClassShortName'] else driver['CarPath']
            state.twitch.status = settings['twitch']['status_tmpl'].format(state.event_type, car_class_name, track_name)
    else:
        state.event_type = None
        if state.twitch:
            state.twitch.status = None

    if ir['SplitTimeInfo']:
        state.first_sector_pct = ir['SplitTimeInfo']['Sectors'][1]['SectorStartPct']
    else:
        state.first_sector_pct = -1

    state.drivers = {}
    state.last_time_update_drivers = -1
    on_cam_change()

def on_cam_change():
    state.last_time_update_lap_ses_time = -1
    state.last_time_update_positions = -1
    state.last_time_update_standing = -1
    state.cur_dist_pct = 0
    state.last_dist_pct = 0
    state.speed_calc_data = []

def update_speed_rpm():
    if ir['CarIdxTrackSurface'][state.cam_car_idx] == irsdk.TrkLoc.NOT_IN_WORLD \
        or (ir['IsReplayPlaying'] and ir['ReplayFrameNumEnd'] > 10):

        if f_speed_rpm.tell() > 0:
            f_speed_rpm.truncate(0)
        return

    if state.my_car_idx == state.cam_car_idx:
        speed = ir['Speed']
        rpm = ir['RPM']
        gear = ir['Gear']
        fuel = ir['FuelLevel']
    else:
        speed = None
        rpm = ir['CarIdxRPM'][state.cam_car_idx]
        gear = ir['CarIdxGear'][state.cam_car_idx]
        fuel = None

    if not rpm is None:
        low = .3
        high = 1 - low
        if rpm >= state.rpm_min:
            rpm = low + high * (rpm - state.rpm_min) / (state.rpm_max - state.rpm_min)
        else:
            rpm = low * rpm / state.rpm_min
        rpm = max(0, min(rpm, 1))
        vert_line = settings['rpm_speed']['vertical_line']
        blocks = settings['rpm_speed']['blocks']
        rpm = vert_line + (
                blocks[-1] * math.ceil(rpm * state.rpm_len - 1) +
                blocks[round(rpm % (1 / state.rpm_len) * state.rpm_len * (len(blocks) - 1))]
            ).ljust(state.rpm_len) + vert_line

    if not gear is None:
        gear = 'R' if gear == -1 else 'N' if gear == 0 else gear

    if speed is None:
        speed = 0
        if len(state.speed_calc_data) > 1:
            diff_pct = state.speed_calc_data[-1][0] - state.speed_calc_data[0][0]
            if diff_pct < 0: diff_pct += 1
            diff_time = max(0, state.speed_calc_data[-1][1] - state.speed_calc_data[0][1])
            if diff_pct > 0 and diff_time > 0:
                speed = max(0, state.track_length * diff_pct / diff_time * 1000)
                if speed > 110:
                    speed = 0
    speed = '{:3.0f}km/h'.format(speed * 3.6)

    fuel = '' if fuel is None else 'Fuel: {:.3f}l'.format(fuel)

    result ='{}{}{}  {}'.format(speed, rpm, gear, fuel)
    logging.debug(result)
    f_speed_rpm.seek(0)
    f_speed_rpm.write(result)
    f_speed_rpm.truncate(f_speed_rpm.tell())

def update_lap_ses_time():
    if state.last_time_update_lap_ses_time > 0 and state.cur_session_time - state.last_time_update_lap_ses_time < .5:
        return
    state.last_time_update_lap_ses_time = state.cur_session_time

    session_type = state.cur_session_type or 'Session Time'
    session_time = state.cur_session_time or 0

    if state.my_car_idx == state.cam_car_idx:
        lap =  ir['Lap']
    else:
        lap =  ir['CarIdxLap'][state.cam_car_idx]

    if not session_time is None:
        m, s = divmod(int(session_time), 60)
        h, m = divmod(m, 60)
        session_time = '{}:{:02}:{:02}'.format(h, m, s) if h else '{}:{:02}'.format(m, s)
        if state.session_time != -1:
            h, m = divmod(int(state.session_time / 60), 60)
            if h or m:
                session_time += '/'
            if h:
                session_time += '{}h'.format(h)
            if m:
                session_time += '{:02}m'.format(m) if h else '{}m'.format(m)

    if not lap is None and not state.session_laps is None:
        if lap < 1 or (ir['IsReplayPlaying'] and ir['ReplayFrameNumEnd'] > 10):
            lap = ''
        elif type(state.session_laps) is int and state.session_laps > 0:
            lap = 'Lap: {}/{}'.format(lap, state.session_laps)
        else:
            lap = 'Lap: %d' % lap

    result = '{}  {}: {}'.format(lap, session_type, session_time)
    logging.debug(result)
    f_lap_ses_time.seek(0)
    f_lap_ses_time.write(result)
    f_lap_ses_time.truncate(f_lap_ses_time.tell())

def update_drivers():
    if state.last_time_update_drivers > 0 and state.cur_session_time - state.last_time_update_drivers < 1:
        return
    state.last_time_update_drivers = state.cur_session_time

    if ir['DriverInfo']:
        for d in ir['DriverInfo']['Drivers']:
            if d['IsSpectator'] or d['UserID'] == -1: continue
            car_idx = d['CarIdx']
            if not car_idx in state.drivers:
                state.drivers[car_idx] = dict(
                    license_class = LICENSE_CLASSES[int(d['LicLevel'] / 5)],
                    safety_rating = '{:.2f}'.format(d['LicSubLevel'] / 100),
                    class_position = 0)
            state.drivers[car_idx]['driver_info'] = d

    if ir['SessionInfo']:
        results_positions = ir['SessionInfo']['Sessions'][state.last_session_num]['ResultsPositions']
        if results_positions:
            for pos in results_positions:
                car_idx = pos['CarIdx']
                if car_idx in state.drivers:
                    state.drivers[car_idx]['position_info'] = pos
                    state.drivers[car_idx]['class_position'] = pos['ClassPosition'] + 1

    if ir['QualifyResultsInfo']:
        qual_positions = ir['QualifyResultsInfo']['Results']
        if qual_positions:
            for pos in qual_positions:
                car_idx = pos['CarIdx']
                if car_idx in state.drivers:
                    state.drivers[car_idx]['qual_info'] = pos


def sort_by_lap_distance(diff):
    if diff < -.5:
        return diff + 1
    elif diff > .5:
        return diff - 1
    return diff

def update_position():
    if state.last_time_update_positions > 0 and state.cur_session_time - state.last_time_update_positions < 1:
        return
    state.last_time_update_positions = state.cur_session_time

    position = []

    if state.cam_car_idx in state.drivers and ir['CarIdxTrackSurface'][state.cam_car_idx] != -1:
        for car_idx, (lap, pct) in enumerate(zip(ir['CarIdxLap'], ir['CarIdxLapDistPct'])):
            if not car_idx in state.drivers: continue
            d = state.drivers[car_idx]
            d['overall_distance'] = lap + pct
            d['lap_distance'] = pct

        is_cur_session_race = state.cur_session_type == 'Race'
        cur_car_lap_dist = state.drivers[state.cam_car_idx]['lap_distance']

        drivers_by_position = sorted([d for d in state.drivers.values() if d['lap_distance'] != -1],
            reverse=True, key=lambda x: sort_by_lap_distance(x['lap_distance'] - cur_car_lap_dist))

        # filter by class
        # cur_car_class_id = state.drivers[state.cam_car_idx]['driver_info']['CarClassID']
        # drivers_by_position = [d for d in drivers_by_position if d['driver_info']['CarClassID'] == cur_car_class_id]

        cur_pos = drivers_by_position.index(state.drivers[state.cam_car_idx])
        pos_format = settings['position']['position_tmpl']

        # next
        if cur_pos == 0:
            position.append('LEADER'.rjust(24) if state.drivers[state.cam_car_idx]['class_position'] == 1 else '')
        else:
            driver = drivers_by_position[cur_pos - 1]
            diff_time = ''
            if 'position_info' in driver:
                last_time = driver['position_info']['LastTime']
                if is_cur_session_race:
                    if last_time != -1:
                        diff_time = '{:.0f}:{:06.3f}'.format(*divmod(last_time, 60))
                else:
                    fastest_time = driver['position_info']['FastestTime']
                    if driver['lap_distance'] < state.first_sector_pct and last_time != -1:
                        diff_time = 'Last {:.0f}:{:06.3f}'.format(*divmod(last_time, 60))
                    elif fastest_time != -1:
                        diff_time = '{:.0f}:{:06.3f}'.format(*divmod(fastest_time, 60))
            position.append(pos_format.format(
                diff_time,
                settings['position']['up_arrow'],
                'P{0[class_position]:2}'.format(driver) if driver['class_position'] > 0 else '',
                driver['driver_info']['CarNumber'],
                driver['license_class'],
                driver['safety_rating'],
                driver['driver_info']['IRating'],
                driver['driver_info']['UserName']))

        # me
        driver = drivers_by_position[cur_pos]
        lap_time = ''
        if 'position_info' in driver:
            last_time = driver['position_info']['LastTime']
            if is_cur_session_race:
                if last_time != -1:
                    lap_time = '{:.0f}:{:06.3f}'.format(*divmod(last_time, 60))
            else:
                fastest_time = driver['position_info']['FastestTime']
                if cur_car_lap_dist < state.first_sector_pct and last_time != -1:
                    lap_time = 'Last {:.0f}:{:06.3f}'.format(*divmod(last_time, 60))
                elif fastest_time != -1:
                    lap_time = '{:.0f}:{:06.3f}'.format(*divmod(fastest_time, 60))

        position.append(pos_format.format(
                lap_time,
                settings['position']['square'],
                'P{0[class_position]:2}'.format(driver) if driver['class_position'] > 0 else '',
                driver['driver_info']['CarNumber'],
                driver['license_class'],
                driver['safety_rating'],
                driver['driver_info']['IRating'],
                driver['driver_info']['UserName']))

        # prev
        if cur_pos == len(drivers_by_position) - 1 or not 'position_info' in drivers_by_position[cur_pos + 1]:
            position.append('')
        else:
            driver = drivers_by_position[cur_pos + 1]
            diff_time = ''
            if 'position_info' in driver:
                last_time = driver['position_info']['LastTime']
                if is_cur_session_race:
                    if last_time != -1:
                        diff_time = '{:.0f}:{:06.3f}'.format(*divmod(last_time, 60))
                else:
                    fastest_time = driver['position_info']['FastestTime']
                    if driver['lap_distance'] < state.first_sector_pct and last_time != -1:
                        diff_time = 'Last {:.0f}:{:06.3f}'.format(*divmod(last_time, 60))
                    elif fastest_time != -1:
                        diff_time = '{:.0f}:{:06.3f}'.format(*divmod(fastest_time, 60))
            position.append(pos_format.format(
                diff_time,
                settings['position']['down_arrow'],
                'P{0[class_position]:2}'.format(driver) if driver['class_position'] > 0 else '',
                driver['driver_info']['CarNumber'],
                driver['license_class'],
                driver['safety_rating'],
                driver['driver_info']['IRating'],
                driver['driver_info']['UserName']))

    result = '\n'.join(position)
    logging.debug('\n%s', result)
    f_position.seek(0)
    f_position.write(result)
    f_position.truncate(f_position.tell())


def update_standing():
    if state.last_time_update_standing > 0 and state.cur_session_time - state.last_time_update_standing < 1:
        return
    state.last_time_update_standing = state.cur_session_time

    standing = []

    is_cur_session_race = state.cur_session_type == 'Race' and ir['SessionState'] >= irsdk.SessionState.RACING
    is_cur_session_qual = 'Qualify' in state.cur_session_type
    use_pos_info = is_cur_session_race or is_cur_session_qual or not ir['QualifyResultsInfo']

    if state.cam_car_idx in state.drivers:
        cur_car_class_id = state.drivers[state.cam_car_idx]['driver_info']['CarClassID']
        drivers_by_position = [d for d in state.drivers.values() if d['driver_info']['CarClassID'] == cur_car_class_id]
    else:
        drivers_by_position = state.drivers.values()

    if use_pos_info:
        drivers_by_position = [d for d in drivers_by_position if 'position_info' in d]
        drivers_by_position = sorted(drivers_by_position, key=lambda x: x['class_position'])
    else:
        drivers_by_position = [d for d in drivers_by_position if 'qual_info' in d]
        drivers_by_position = sorted(drivers_by_position, key=lambda x: x['qual_info']['Position'])

    for i, driver in enumerate(drivers_by_position):
        diff_time = ''
        if use_pos_info:
            driver_pos_info = driver['position_info']
            if is_cur_session_race:
                leader_pos_info = drivers_by_position[0]['position_info']
                leader_last_lap_time = leader_pos_info['LastTime']

                car_idx = driver['position_info']['CarIdx']
                is_in_pit = ir['CarIdxOnPitRoad'][car_idx]
                laps_complete = driver_pos_info['LapsComplete']

                if i == 0:
                    diff_time = '{:>5} {:>5}'.format('LAP', 'PIT' if is_in_pit else laps_complete)
                else:
                    prev_driver_pos_info = drivers_by_position[i - 1]['position_info']
                    diff_laps = leader_pos_info['LapsComplete'] - laps_complete
                    diff_laps_rel = prev_driver_pos_info['LapsComplete'] - laps_complete

                    gap = driver_pos_info['Time'] - leader_pos_info['Time']
                    gap_str = ''

                    if gap >= 0 and laps_complete:
                        if diff_laps <= 0 or \
                            (diff_laps == 1 and (leader_last_lap_time == -1 or gap < leader_last_lap_time)):
                            gap_str = '{:.1f}'.format(gap)
                        elif ir['SessionState'] < irsdk.SessionState.CHECKERED and \
                            diff_laps > 0 and leader_last_lap_time != -1 and \
                            math.ceil(gap / leader_last_lap_time) == diff_laps:
                            gap_str = '{:4}L'.format(diff_laps - 1)
                        elif diff_laps > 0:
                            gap_str = '{:4}L'.format(diff_laps)

                    if not gap_str and diff_laps > 1:
                        gap_str = '{:4}L'.format(diff_laps)

                    inter = driver_pos_info['Time'] - prev_driver_pos_info['Time']
                    inter_str = ''

                    if is_in_pit:
                        inter_str = 'PIT'
                    elif inter >= 0 and laps_complete:
                        if diff_laps_rel <= 0 or \
                            (diff_laps_rel == 1 and (leader_last_lap_time == -1 or inter < leader_last_lap_time)):
                            inter_str = '{:.1f}'.format(inter)
                        elif ir['SessionState'] < irsdk.SessionState.CHECKERED and \
                            diff_laps_rel > 0 and leader_last_lap_time != -1 and \
                            math.ceil(inter / leader_last_lap_time) == diff_laps_rel:
                            inter_str = '{:4}L'.format(diff_laps_rel - 1)
                        elif diff_laps_rel > 0:
                            inter_str = '{:4}L'.format(diff_laps_rel)

                    if not inter_str and diff_laps_rel > 1:
                        inter_str = '{:4}L'.format(diff_laps_rel)

                    diff_time = '{:>5} {:>5}'.format(gap_str, inter_str)
            else:
                fastest_time = driver_pos_info['FastestTime']
                if fastest_time != -1:
                    diff_time = '{:.0f}:{:06.3f}'.format(*divmod(fastest_time, 60))
        else:
            fastest_time = driver['qual_info']['FastestTime']
            if fastest_time > 0:
                diff_time = '{:.0f}:{:06.3f}'.format(*divmod(fastest_time, 60))

        standing.append((driver, diff_time))

    if len(standing):
        max_abbrev_len = max(len(driver['driver_info']['AbbrevName']) for driver, _ in standing) - 3 # 3 = last ', X'

        if is_cur_session_race:
            standing_header = '{:>4} {:>3} {} {:>5} {:>5}'.format('Pos', '#', ' ' * max_abbrev_len, 'Gap', 'Int')
        else:
            standing_header = '{:>4} {:>3} {} {:>8}'.format('Pos', '#', ' ' * max_abbrev_len, 'Lap Time')

        standing_fmt = '{0:1}{1:3} {2[driver_info][CarNumber]:>3} {3:%d} {4}' % max_abbrev_len
        cur_driver_index = -1
        for i, (driver, diff_time) in enumerate(standing):
            r_arr = ''
            if driver['driver_info']['CarIdx'] == state.cam_car_idx:
                r_arr = settings['standing']['right_arrow']
                cur_driver_index = i
            pos = driver['class_position'] if use_pos_info else driver['qual_info']['Position'] + 1
            driver_name = driver['driver_info']['AbbrevName'].rsplit(',', 1)[0]
            standing[i] = standing_fmt.format(r_arr, pos, driver, driver_name, diff_time)

        max_standing = settings['standing']['max']
        window = settings['standing']['window']
        if len(standing) <= max_standing:
            pass
        elif cur_driver_index == -1 or cur_driver_index < max_standing - int(window / 2):
            standing = standing[:max_standing]
        else:
            standing = standing[:max_standing - 1 - window + max(0, int(window / 2) + cur_driver_index + 1 - len(standing))] + \
                [settings['standing']['horizontal_bar'] * len(standing_header)] + \
                standing[cur_driver_index - int(window / 2) : cur_driver_index + math.ceil(window / 2)]

        result = standing_header + '\n' + '\n'.join(standing)
    else:
        result = ''

    logging.debug('\n%s', result)
    f_standing.seek(0)
    f_standing.write(result)
    f_standing.truncate(f_standing.tell())


def update_twitch():
    tw_state = state.twitch

    # update twitch status
    if tw_state.oauth_token and tw_state.status and tw_state.status != tw_state.last_status:
        if not tw_state.twreq_status:
            logging.info('start update twitch status')
            tw_state.twreq_status = twitch.TwitchAPIRequest(twitch.TWITCH_API_CHANNELS % tw_state.channel.lower(), 'PUT',
                {'channel[status]': tw_state.status}, oauth_token=tw_state.oauth_token)
        if not tw_state.twreq_status.is_alive():
            if tw_state.twreq_status.error:
                logging.warn('twitch status error: %s', tw_state.twreq_status.error)
            elif tw_state.twreq_status.result:
                logging.info('twitch status updated: %s', tw_state.twreq_status.result['status'])
                tw_state.last_status = tw_state.status
            tw_state.twreq_status = None

    # update viewers and followers
    data_changed = False

    if tw_state.pending:
        logging.debug('twitch pending')
        tw_state.pending = (tw_state.twreq_stream and tw_state.twreq_stream.is_alive()) or \
            (tw_state.twreq_follows and tw_state.twreq_follows.is_alive())
        if not tw_state.pending:
            tw_state.last_update = time.time()
            data_changed = True

        if tw_state.twreq_stream and not tw_state.twreq_stream.is_alive():
            if tw_state.twreq_stream.error:
                logging.warn('twitch stream error: %s', tw_state.twreq_stream.error)
            elif tw_state.twreq_stream.result:
                if tw_state.twreq_stream.result['stream']:
                    tw_state.last_viewers = tw_state.twreq_stream.result['stream']['viewers']
                else:
                    tw_state.last_viewers = 0
            tw_state.twreq_stream = None

        if tw_state.twreq_follows and not tw_state.twreq_follows.is_alive():
            if tw_state.twreq_follows.error:
                logging.warn('twitch follows error: %s', tw_state.twreq_follows.error)
            elif tw_state.twreq_follows.result:
                tw_state.last_followers = tw_state.twreq_follows.result['_total']
                if tw_state.twreq_follows.result['follows']:
                    tw_state.last_follower = tw_state.twreq_follows.result['follows'][0]['user']['display_name']
            tw_state.twreq_follows = None

    if not tw_state.pending and time.time() - tw_state.last_update > 10:
        logging.debug('twitch start requests')
        tw_state.twreq_stream = twitch.TwitchAPIRequest(twitch.TWITCH_API_STREAMS % tw_state.channel.lower())
        tw_state.twreq_follows = twitch.TwitchAPIRequest(twitch.TWITCH_API_CHANNELS_FOLLOWS % tw_state.channel.lower(), data=dict(limit=1))
        tw_state.pending = True

    if data_changed:
        result = settings['twitch']['lates_follower_tmpl'].format(tw_state.last_follower) if tw_state.last_follower else ''
        logging.debug(result)
        f_twitch_last_follower.seek(0)
        f_twitch_last_follower.write(result)
        f_twitch_last_follower.truncate(f_twitch_last_follower.tell())

        result = settings['twitch']['viewers_followers_tmpl'].format(tw_state.last_viewers, tw_state.last_followers)
        logging.debug(result)
        f_twitch_viewers_followers.seek(0)
        f_twitch_viewers_followers.write(result)
        f_twitch_viewers_followers.truncate(f_twitch_viewers_followers.tell())



def main():
    global state

    if state.twitch:
        update_twitch()

    if state.is_connected and (not ir.is_initialized or not ir.is_connected):
        state.is_connected = False
        ir.shutdown()
        for f in [f_speed_rpm, f_lap_ses_time, f_position, f_standing]:
            f.truncate(0)
        logging.info('IRSDK disconnected')
        tw_state = state.twitch
        state = State()
        state.twitch = tw_state
    elif not state.is_connected and (ir.is_initialized or ir.is_connected or ir.startup()):
        state.is_connected = True
        logging.info('IRSDK connected')

    if not state.is_connected:
        time.sleep(2)
        return

    state.cur_session_time = ir['SessionTime']

    # session changed
    if state.last_session_num != ir['SessionNum'] or \
        state.last_session_state != ir['SessionState'] or \
        state.rpm_min == -1 or state.rpm_max == -1 or \
        state.track_length == -1 or \
        state.first_sector_pct == -1 or \
        not state.cur_session_type or \
        (state.twitch and not state.twitch.status):

        state.last_session_num = ir['SessionNum']
        state.last_session_state = ir['SessionState']
        try:
            on_session_change()
        except:
            state.last_session_num = -1
            logging.exception('error in on session change')

    # cam changed
    if state.cam_car_idx != ir['CamCarIdx']:
        state.cam_car_idx = ir['CamCarIdx']
        try:
            on_cam_change()
        except:
            state.cam_car_idx = -1
            logging.exception('error in on cam change')

    state.last_dist_pct = state.cur_dist_pct
    state.cur_dist_pct = ir['CarIdxLapDistPct'][state.cam_car_idx]
    state.speed_calc_data.append((state.cur_dist_pct, state.cur_session_time))
    state.speed_calc_data = state.speed_calc_data[-10:]

    update_speed_rpm()
    update_lap_ses_time()
    update_drivers()
    update_position()
    update_standing()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', help='output verbosity', action='count', default=2)
    parser.add_argument('-s', '--silent', help='turn off display log', action='store_true', default=False)
    parser.add_argument('-nt', '--no-twitch', help='turn off twitch update', action='store_true')
    parser.add_argument('-V', '--version', action='version', version='iRacing Text Overlay %s' % VERSION, help='show version and exit')
    parser.add_argument('--test', help='use test file as irsdk mmap')
    parser.add_argument('--dump', help='dump irsdk mmap to file')
    args = parser.parse_args()

    logging_handlers = [logging.handlers.RotatingFileHandler('log', maxBytes=1 * 1024 * 1024, encoding='utf-8')]
    if not args.silent:
        logging_handlers.append(logging.StreamHandler())

    logging.basicConfig(format='{asctime} {levelname:>8}: {message}', datefmt='%Y-%m-%d %H:%M:%S', style='{',
        handlers=logging_handlers, level=[logging.WARN, logging.INFO, logging.DEBUG][min(args.verbose - 1, 2)])

    logging.info('iRacing Text Overlay %s' % VERSION)

    settings = None
    try:
        settings = json.loads(re.sub(r'^\s*\/\/.*', '', open('settings.json', 'r', encoding='utf-8').read(), flags=re.M))
    except FileNotFoundError:
        shutil.copy('settings.tmpl', 'settings.json')
        logging.info('Settings file created')
        sys.exit(0)

    if settings:
        logging.info('Settings file loaded')
    else:
        logging.fatal('No settings file')
        sys.exit(0)

    ir = irsdk.IRSDK()
    ir.startup(test_file=args.test, dump_to=args.dump)

    if args.dump:
        sys.exit(0)

    f_speed_rpm = open('speed_rpm.txt', 'w', encoding='utf-8')
    f_lap_ses_time = open('lap_ses_time.txt', 'w', encoding='utf-8')
    f_position = open('position.txt', 'w', encoding='utf-8')
    f_standing = open('standing.txt', 'w', encoding='utf-8')

    state = State()

    if not args.test and not args.no_twitch and settings['twitch']['channel']:
        state.twitch = TwitchState()
        state.twitch.channel = settings['twitch']['channel']
        state.twitch.oauth_token = settings['twitch']['access_token']
        f_twitch_last_follower = open('twitch_last_follower.txt', 'w', encoding='utf-8')
        f_twitch_viewers_followers = open('twitch_viewers_followers.txt', 'w', encoding='utf-8')

    try:
        if args.test or args.dump:
            main()
        else:
            while True:
                main()
                time.sleep(1/25)
    except KeyboardInterrupt:
        pass
    except:
        logging.exception('')
