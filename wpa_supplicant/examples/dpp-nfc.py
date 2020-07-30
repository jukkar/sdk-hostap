#!/usr/bin/python3
#
# Example nfcpy to wpa_supplicant wrapper for DPP NFC operations
# Copyright (c) 2012-2013, Jouni Malinen <j@w1.fi>
# Copyright (c) 2019-2020, The Linux Foundation
#
# This software may be distributed under the terms of the BSD license.
# See README for more details.

import os
import struct
import sys
import time
import threading
import argparse

import nfc
import ndef

import logging

scriptsdir = os.path.dirname(os.path.realpath(sys.modules[__name__].__file__))
sys.path.append(os.path.join(scriptsdir, '..', '..', 'wpaspy'))
import wpaspy

wpas_ctrl = '/var/run/wpa_supplicant'
ifname = None
init_on_touch = False
in_raw_mode = False
prev_tcgetattr = 0
no_input = False
srv = None
continue_loop = True
terminate_now = False
summary_file = None
success_file = None
my_crn_ready = False
my_crn = None
peer_crn = None
hs_sent = False
netrole = None
operation_success = False
mutex = threading.Lock()
no_alt_proposal = False

C_NORMAL = '\033[0m'
C_RED = '\033[91m'
C_GREEN = '\033[92m'
C_BLUE = '\033[94m'
C_MAGENTA = '\033[95m'
C_CYAN = '\033[96m'

def summary(txt, color=None):
    with mutex:
        if color:
            print(color + txt + C_NORMAL)
        else:
            print(txt)
        if summary_file:
            with open(summary_file, 'a') as f:
                f.write(txt + "\n")

def success_report(txt):
    summary(txt)
    if success_file:
        with open(success_file, 'a') as f:
            f.write(txt + "\n")

def wpas_connect():
    ifaces = []
    if os.path.isdir(wpas_ctrl):
        try:
            ifaces = [os.path.join(wpas_ctrl, i) for i in os.listdir(wpas_ctrl)]
        except OSError as error:
            summary("Could not find wpa_supplicant: %s", str(error))
            return None

    if len(ifaces) < 1:
        summary("No wpa_supplicant control interface found")
        return None

    for ctrl in ifaces:
        if ifname and ifname not in ctrl:
            continue
        if os.path.basename(ctrl).startswith("p2p-dev-"):
            # skip P2P management interface
            continue
        try:
            summary("Trying to use control interface " + ctrl)
            wpas = wpaspy.Ctrl(ctrl)
            return wpas
        except Exception as e:
            pass
    summary("Could not connect to wpa_supplicant")
    return None

def dpp_nfc_uri_process(uri):
    wpas = wpas_connect()
    if wpas is None:
        return False
    peer_id = wpas.request("DPP_NFC_URI " + uri)
    if "FAIL" in peer_id:
        summary("Could not parse DPP URI from NFC URI record", color=C_RED)
        return False
    peer_id = int(peer_id)
    summary("peer_id=%d for URI from NFC Tag: %s" % (peer_id, uri))
    cmd = "DPP_AUTH_INIT peer=%d" % peer_id
    global enrollee_only, configurator_only, config_params
    if enrollee_only:
        cmd += " role=enrollee"
    elif configurator_only:
        cmd += " role=configurator"
    if config_params:
        cmd += " " + config_params
    summary("Initiate DPP authentication: " + cmd)
    res = wpas.request(cmd)
    if "OK" not in res:
        summary("Failed to initiate DPP Authentication", color=C_RED)
        return False
    summary("DPP Authentication initiated")
    return True

def dpp_hs_tag_read(record):
    wpas = wpas_connect()
    if wpas is None:
        return False
    summary(record)
    if len(record.data) < 5:
        summary("Too short DPP HS", color=C_RED)
        return False
    if record.data[0] != 0:
        summary("Unexpected URI Identifier Code", color=C_RED)
        return False
    uribuf = record.data[1:]
    try:
        uri = uribuf.decode()
    except:
        summary("Invalid URI payload", color=C_RED)
        return False
    summary("URI: " + uri)
    if not uri.startswith("DPP:"):
        summary("Not a DPP URI", color=C_RED)
        return False
    return dpp_nfc_uri_process(uri)

def get_status(wpas, extra=None):
    if extra:
        extra = "-" + extra
    else:
        extra = ""
    res = wpas.request("STATUS" + extra)
    lines = res.splitlines()
    vals = dict()
    for l in lines:
        try:
            [name, value] = l.split('=', 1)
        except ValueError:
            summary("Ignore unexpected status line: %s" % l)
            continue
        vals[name] = value
    return vals

def get_status_field(wpas, field, extra=None):
    vals = get_status(wpas, extra)
    if field in vals:
        return vals[field]
    return None

def own_addr(wpas):
    addr = get_status_field(wpas, "address")
    if addr is None:
        addr = get_status_field(wpas, "bssid[0]")
    return addr

def dpp_bootstrap_gen(wpas, type="qrcode", chan=None, mac=None, info=None,
                      curve=None, key=None):
    cmd = "DPP_BOOTSTRAP_GEN type=" + type
    if chan:
        cmd += " chan=" + chan
    if mac:
        if mac is True:
            mac = own_addr(wpas)
        if mac is None:
            summary("Could not determine local MAC address for bootstrap info")
        else:
            cmd += " mac=" + mac.replace(':', '')
    if info:
        cmd += " info=" + info
    if curve:
        cmd += " curve=" + curve
    if key:
        cmd += " key=" + key
    res = wpas.request(cmd)
    if "FAIL" in res:
        raise Exception("Failed to generate bootstrapping info")
    return int(res)

def wpas_get_nfc_uri(start_listen=True, pick_channel=False, chan_override=None):
    listen_freq = 2412
    wpas = wpas_connect()
    if wpas is None:
        return None
    global own_id, chanlist
    if chan_override:
        chan = chan_override
    else:
        chan = chanlist
    if chan is None and get_status_field(wpas, "bssid[0]"):
        freq = get_status_field(wpas, "freq")
        if freq:
            freq = int(freq)
            if freq >= 2412 and freq <= 2462:
                chan = "81/%d" % ((freq - 2407) / 5)
                summary("Use current AP operating channel (%d MHz) as the URI channel list (%s)" % (freq, chan))
                listen_freq = freq
    if chan is None and pick_channel:
        chan = "81/6"
        summary("Use channel 2437 MHz since no other preference provided")
        listen_freq = 2437
    own_id = dpp_bootstrap_gen(wpas, type="nfc-uri", chan=chan, mac=True)
    res = wpas.request("DPP_BOOTSTRAP_GET_URI %d" % own_id).rstrip()
    if "FAIL" in res:
        return None
    if start_listen:
        cmd = "DPP_LISTEN %d" % listen_freq
        global netrole
        if netrole:
            cmd += " netrole=" + netrole
        res2 = wpas.request(cmd)
        if "OK" not in res2:
            raise Exception("Failed to start listen operation (%s)" % cmd)
    return res

def wpas_report_handover_req(uri):
    wpas = wpas_connect()
    if wpas is None:
        return None
    global own_id
    cmd = "DPP_NFC_HANDOVER_REQ own=%d uri=%s" % (own_id, uri)
    return wpas.request(cmd)

def wpas_report_handover_sel(uri):
    wpas = wpas_connect()
    if wpas is None:
        return None
    global own_id
    cmd = "DPP_NFC_HANDOVER_SEL own=%d uri=%s" % (own_id, uri)
    return wpas.request(cmd)

def dpp_handover_client(llc, alt=False):
    chan_override = None
    global alt_proposal_used
    if alt:
        global altchanlist
        chan_override = altchanlist
        alt_proposal_used = True
    global test_uri, test_alt_uri
    if test_uri:
        summary("TEST MODE: Using specified URI (alt=%s)" % str(alt))
        uri = test_alt_uri if alt else test_uri
    else:
        uri = wpas_get_nfc_uri(start_listen=False, chan_override=chan_override)
    if uri is None:
        summary("Cannot start handover client - no bootstrap URI available",
                color=C_RED)
        return
    uri = ndef.UriRecord(uri)
    summary("NFC URI record for DPP: " + str(uri))
    carrier = ndef.Record('application/vnd.wfa.dpp', 'A', uri.data)
    global test_crn
    if test_crn:
        prev, = struct.unpack('>H', test_crn)
        summary("TEST MODE: Use specified crn %d" % prev)
        crn = test_crn
        test_crn = struct.pack('>H', prev + 0x10)
    else:
        crn = os.urandom(2)
    hr = ndef.HandoverRequestRecord(version="1.4", crn=crn)
    hr.add_alternative_carrier('active', carrier.name)
    message = [hr, carrier]
    summary("NFC Handover Request message for DPP: " + str(message))

    global peer_crn
    if peer_crn is not None and not alt:
        summary("NFC handover request from peer was already received - do not send own")
        return
    client = nfc.handover.HandoverClient(llc)
    try:
        summary("Trying to initiate NFC connection handover")
        client.connect()
        summary("Connected for handover")
    except nfc.llcp.ConnectRefused:
        summary("Handover connection refused")
        client.close()
        return
    except Exception as e:
        summary("Other exception: " + str(e))
        client.close()
        return

    if peer_crn is not None and not alt:
        summary("NFC handover request from peer was already received - do not send own")
        client.close()
        return

    summary("Sending handover request")

    global my_crn, my_crn_ready, hs_sent
    my_crn_ready = True

    if not client.send_records(message):
        my_crn_ready = False
        summary("Failed to send handover request", color=C_RED)
        client.close()
        return

    my_crn, = struct.unpack('>H', crn)

    summary("Receiving handover response")
    try:
        message = client.recv_records(timeout=3.0)
    except Exception as e:
        # This is fine if we are the handover selector
        if hs_sent:
            summary("Client receive failed as expected since I'm the handover server: %s" % str(e))
        elif alt_proposal_used and not alt:
            summary("Client received failed for initial proposal as expected since alternative proposal was also used: %s" % str(e))
        else:
            summary("Client receive failed: %s" % str(e), color=C_RED)
        message = None
    if message is None:
        if hs_sent:
            summary("No response received as expected since I'm the handover server")
        elif alt_proposal_used and not alt:
            summary("No response received for initial proposal as expected since alternative proposal was also used")
        else:
            summary("No response received", color=C_RED)
        client.close()
        return
    summary("Received message: " + str(message))
    if len(message) < 1 or \
       not isinstance(message[0], ndef.HandoverSelectRecord):
        summary("Response was not Hs - received: " + message.type)
        client.close()
        return

    summary("Received handover select message")
    summary("alternative carriers: " + str(message[0].alternative_carriers))

    if alt_proposal_used and not alt:
        summary("Ignore received handover select for the initial proposal since alternative proposal was sent")
        client.close()
        return

    dpp_found = False
    for carrier in message:
        if isinstance(carrier, ndef.HandoverSelectRecord):
            continue
        summary("Remote carrier type: " + carrier.type)
        if carrier.type == "application/vnd.wfa.dpp":
            if len(carrier.data) == 0 or carrier.data[0] != 0:
                summary("URI Identifier Code 'None' not seen", color=C_RED)
                continue
            summary("DPP carrier type match - send to wpa_supplicant")
            dpp_found = True
            uri = carrier.data[1:].decode("utf-8")
            summary("DPP URI: " + uri)
            if test_uri:
                summary("TEST MODE: Fake processing")
                break
            res = wpas_report_handover_sel(uri)
            if res is None or "FAIL" in res:
                summary("DPP handover report rejected", color=C_RED)
                break

            success_report("DPP handover reported successfully (initiator)")
            summary("peer_id=" + res)
            peer_id = int(res)
            wpas = wpas_connect()
            if wpas is None:
                break

            global enrollee_only
            global config_params
            if enrollee_only:
                extra = " role=enrollee"
            elif config_params:
                extra = " role=configurator " + config_params
            else:
                # TODO: Single Configurator instance
                res = wpas.request("DPP_CONFIGURATOR_ADD")
                if "FAIL" in res:
                    summary("Failed to initiate Configurator", color=C_RED)
                    break
                conf_id = int(res)
                extra = " conf=sta-dpp configurator=%d" % conf_id
            global own_id
            summary("Initiate DPP authentication")
            cmd = "DPP_AUTH_INIT peer=%d own=%d" % (peer_id, own_id)
            cmd += extra
            res = wpas.request(cmd)
            if "FAIL" in res:
                summary("Failed to initiate DPP authentication", color=C_RED)
            break

    global no_alt_proposal
    if not dpp_found and no_alt_proposal:
        summary("DPP carrier not seen in response - do not allow alternative proposal anymore")
    elif not dpp_found:
        summary("DPP carrier not seen in response - allow peer to initiate a new handover with different parameters")
        my_crn_ready = False
        my_crn = None
        peer_crn = None
        hs_sent = False
        client.close()
        summary("Returning from dpp_handover_client")
        return

    summary("Remove peer")
    client.close()
    summary("Done with handover")
    global only_one
    if only_one:
        print("only_one -> stop loop")
        global continue_loop
        continue_loop = False

    global no_wait
    if no_wait:
        print("Trying to exit..")
        global terminate_now
        terminate_now = True

    summary("Returning from dpp_handover_client")

class HandoverServer(nfc.handover.HandoverServer):
    def __init__(self, llc):
        super(HandoverServer, self).__init__(llc)
        self.sent_carrier = None
        self.ho_server_processing = False
        self.success = False
        self.try_own = False
        self.llc = llc

    def process_handover_request_message(self, records):
        self.ho_server_processing = True
        global in_raw_mode
        was_in_raw_mode = in_raw_mode
        clear_raw_mode()
        if was_in_raw_mode:
            print("\n")
        summary("HandoverServer - request received: " + str(records))

        global my_crn, peer_crn, my_crn_ready

        for carrier in records:
            if not isinstance(carrier, ndef.HandoverRequestRecord):
                continue
            if carrier.collision_resolution_number:
                peer_crn = carrier.collision_resolution_number
                summary("peer_crn: %d" % peer_crn)

        if my_crn is None and my_crn_ready:
            summary("Still trying to send own handover request - wait a moment to see if that succeeds before checking crn values")
            for i in range(10):
                if my_crn is not None:
                    break
                time.sleep(0.01)
        if my_crn is not None:
            summary("my_crn: %d" % my_crn)

        if my_crn is not None and peer_crn is not None:
            if my_crn == peer_crn:
                summary("Same crn used - automatic collision resolution failed")
                # TODO: Should generate a new Handover Request message
                return ''
            if ((my_crn & 1) == (peer_crn & 1) and my_crn > peer_crn) or \
               ((my_crn & 1) != (peer_crn & 1) and my_crn < peer_crn):
                summary("I'm the Handover Selector Device")
                pass
            else:
                summary("Peer is the Handover Selector device")
                summary("Ignore the received request.")
                return ''

        hs = ndef.HandoverSelectRecord('1.4')
        sel = [hs]

        found = False

        for carrier in records:
            if isinstance(carrier, ndef.HandoverRequestRecord):
                continue
            summary("Remote carrier type: " + carrier.type)
            if carrier.type == "application/vnd.wfa.dpp":
                summary("DPP carrier type match - add DPP carrier record")
                if len(carrier.data) == 0 or carrier.data[0] != 0:
                    summary("URI Identifier Code 'None' not seen", color=C_RED)
                    continue
                uri = carrier.data[1:].decode("utf-8")
                summary("Received DPP URI: " + uri)

                global test_uri, test_alt_uri
                if test_uri:
                    summary("TEST MODE: Using specified URI")
                    data = test_sel_uri if test_sel_uri else test_uri
                else:
                    data = wpas_get_nfc_uri(start_listen=False,
                                            pick_channel=True)
                summary("Own URI (pre-processing): %s" % data)

                if test_uri:
                    summary("TEST MODE: Fake processing")
                    res = "OK"
                else:
                    res = wpas_report_handover_req(uri)
                if res is None or "FAIL" in res:
                    summary("DPP handover request processing failed",
                            color=C_RED)
                    global altchanlist
                    if altchanlist:
                        data = wpas_get_nfc_uri(start_listen=False,
                                                chan_override=altchanlist)
                        summary("Own URI (try another channel list): %s" % data)
                    continue

                if test_alt_uri:
                    summary("TEST MODE: Reject initial proposal")
                    continue

                found = True

                if not test_uri:
                    wpas = wpas_connect()
                    if wpas is None:
                        continue
                    global own_id
                    data = wpas.request("DPP_BOOTSTRAP_GET_URI %d" % own_id).rstrip()
                    if "FAIL" in data:
                        continue
                summary("Own URI (post-processing): %s" % data)
                uri = ndef.UriRecord(data)
                summary("Own bootstrapping NFC URI record: " + str(uri))

                if not test_uri:
                    info = wpas.request("DPP_BOOTSTRAP_INFO %d" % own_id)
                    freq = None
                    for line in info.splitlines():
                        if line.startswith("use_freq="):
                            freq = int(line.split('=')[1])
                    if freq is None or freq == 0:
                        summary("No channel negotiated over NFC - use channel 6")
                        freq = 2437
                    else:
                        summary("Negotiated channel: %d MHz" % freq)
                    if get_status_field(wpas, "bssid[0]"):
                        summary("Own AP freq: %s MHz" % str(get_status_field(wpas, "freq")))
                        if get_status_field(wpas, "beacon_set", extra="DRIVER") is None:
                            summary("Enable beaconing to have radio ready for RX")
                            wpas.request("DISABLE")
                            wpas.request("SET start_disabled 0")
                            wpas.request("ENABLE")
                    cmd = "DPP_LISTEN %d" % freq
                    global enrollee_only
                    global configurator_only
                    if enrollee_only:
                        cmd += " role=enrollee"
                    elif configurator_only:
                        cmd += " role=configurator"
                    summary(cmd)
                    res = wpas.request(cmd)
                    if "OK" not in res:
                        summary("Failed to start DPP listen", color=C_RED)
                        break

                carrier = ndef.Record('application/vnd.wfa.dpp', 'A', uri.data)
                summary("Own DPP carrier record: " + str(carrier))
                hs.add_alternative_carrier('active', carrier.name)
                sel = [hs, carrier]
                break

        global hs_sent, no_alt_proposal
        summary("Sending handover select: " + str(sel))
        if found:
            summary("Handover completed successfully")
            self.success = True
            hs_sent = True
        elif no_alt_proposal:
            summary("Do not try alternative proposal anymore - handover failed",
                    color=C_RED)
            hs_sent = True
        else:
            summary("Try to initiate with alternative parameters")
            self.try_own = True
            hs_sent = False
            no_alt_proposal = True
            threading.Thread(target=llcp_worker, args=(self.llc, True)).start()
        return sel

def clear_raw_mode():
    import sys, tty, termios
    global prev_tcgetattr, in_raw_mode
    if not in_raw_mode:
        return
    fd = sys.stdin.fileno()
    termios.tcsetattr(fd, termios.TCSADRAIN, prev_tcgetattr)
    in_raw_mode = False

def getch():
    import sys, tty, termios, select
    global prev_tcgetattr, in_raw_mode
    fd = sys.stdin.fileno()
    prev_tcgetattr = termios.tcgetattr(fd)
    ch = None
    try:
        tty.setraw(fd)
        in_raw_mode = True
        [i, o, e] = select.select([fd], [], [], 0.05)
        if i:
            ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, prev_tcgetattr)
        in_raw_mode = False
    return ch

def dpp_tag_read(tag):
    success = False
    for record in tag.ndef.records:
        summary(record)
        summary("record type " + record.type)
        if record.type == "application/vnd.wfa.dpp":
            summary("DPP HS tag - send to wpa_supplicant")
            success = dpp_hs_tag_read(record)
            break
        if isinstance(record, ndef.UriRecord):
            summary("URI record: uri=" + record.uri)
            summary("URI record: iri=" + record.iri)
            if record.iri.startswith("DPP:"):
                summary("DPP URI")
                if not dpp_nfc_uri_process(record.iri):
                    break
                success = True
            else:
                summary("Ignore unknown URI")
            break

    if success:
        success_report("Tag read succeeded")

    return success

def rdwr_connected_write_tag(tag):
    summary("Tag found - writing - " + str(tag))
    if not tag.ndef:
        summary("Not a formatted NDEF tag", color=C_RED)
        return
    if not tag.ndef.is_writeable:
        summary("Not a writable tag", color=C_RED)
        return
    global dpp_tag_data
    if tag.ndef.capacity < len(dpp_tag_data):
        summary("Not enough room for the message")
        return
    try:
        tag.ndef.records = dpp_tag_data
    except ValueError as e:
        summary("Writing the tag failed: %s" % str(e), color=C_RED)
        return
    success_report("Tag write succeeded")
    summary("Tag writing completed - remove tag", color=C_GREEN)
    global only_one, operation_success
    operation_success = True
    if only_one:
        global continue_loop
        continue_loop = False
    global dpp_sel_wait_remove
    return dpp_sel_wait_remove

def write_nfc_uri(clf, wait_remove=True):
    summary("Write NFC URI record")
    data = wpas_get_nfc_uri()
    if data is None:
        summary("Could not get NFC URI from wpa_supplicant", color=C_RED)
        return

    global dpp_sel_wait_remove
    dpp_sel_wait_remove = wait_remove
    summary("URI: %s" % data)
    uri = ndef.UriRecord(data)
    summary(uri)

    summary("Touch an NFC tag to write URI record", color=C_CYAN)
    global dpp_tag_data
    dpp_tag_data = [uri]
    clf.connect(rdwr={'on-connect': rdwr_connected_write_tag})

def write_nfc_hs(clf, wait_remove=True):
    summary("Write NFC Handover Select record on a tag")
    data = wpas_get_nfc_uri()
    if data is None:
        summary("Could not get NFC URI from wpa_supplicant", color=C_RED)
        return

    global dpp_sel_wait_remove
    dpp_sel_wait_remove = wait_remove
    summary("URI: %s" % data)
    uri = ndef.UriRecord(data)
    summary(uri)
    carrier = ndef.Record('application/vnd.wfa.dpp', 'A', uri.data)
    hs = ndef.HandoverSelectRecord('1.4')
    hs.add_alternative_carrier('active', carrier.name)
    summary(hs)
    summary(carrier)

    summary("Touch an NFC tag to write HS record", color=C_CYAN)
    global dpp_tag_data
    dpp_tag_data = [hs, carrier]
    summary(dpp_tag_data)
    clf.connect(rdwr={'on-connect': rdwr_connected_write_tag})

def rdwr_connected(tag):
    global only_one, no_wait
    summary("Tag connected: " + str(tag))

    if tag.ndef:
        summary("NDEF tag: " + tag.type)
        summary(tag.ndef.records)
        success = dpp_tag_read(tag)
        if only_one and success:
            global continue_loop
            continue_loop = False
    else:
        summary("Not an NDEF tag - remove tag", color=C_RED)
        return True

    return not no_wait

def llcp_worker(llc, try_alt):
    print("Start of llcp_worker()")
    if try_alt:
        summary("Starting handover client (try_alt)")
        dpp_handover_client(llc, alt=True)
        summary("Exiting llcp_worker thread (try_alt)")
        return
    global init_on_touch
    if init_on_touch:
        summary("Starting handover client (init_on_touch)")
        dpp_handover_client(llc)
        summary("Exiting llcp_worker thread (init_on_touch)")
        return

    global no_input
    if no_input:
        summary("Wait for handover to complete")
    else:
        print("Wait for handover to complete - press 'i' to initiate")
    global srv
    global wait_connection
    while not wait_connection and srv.sent_carrier is None:
        if srv.try_own:
            srv.try_own = False
            summary("Try to initiate another handover with own parameters")
            global peer_crn, my_crn, my_crn_ready, hs_sent
            my_crn_ready = False
            my_crn = None
            peer_crn = None
            hs_sent = False
            dpp_handover_client(llc, alt=True)
            summary("Exiting llcp_worker thread (retry with own parameters)")
            return
        if srv.ho_server_processing:
            time.sleep(0.025)
        elif no_input:
            time.sleep(0.5)
        else:
            res = getch()
            if res != 'i':
                continue
            clear_raw_mode()
            summary("Starting handover client")
            dpp_handover_client(llc)
            summary("Exiting llcp_worker thread (manual init)")
            return

    global in_raw_mode
    was_in_raw_mode = in_raw_mode
    clear_raw_mode()
    if was_in_raw_mode:
        print("\r")
    summary("Exiting llcp_worker thread")

def llcp_startup(llc):
    summary("Start LLCP server")
    global srv
    srv = HandoverServer(llc)
    return llc

def llcp_connected(llc):
    summary("P2P LLCP connected")
    global wait_connection, my_crn, peer_crn, my_crn_ready, hs_sent
    global no_alt_proposal, alt_proposal_used
    wait_connection = False
    my_crn_ready = False
    my_crn = None
    peer_crn = None
    hs_sent = False
    no_alt_proposal = False
    alt_proposal_used = False
    global srv
    srv.start()
    if init_on_touch or not no_input:
        threading.Thread(target=llcp_worker, args=(llc, False)).start()
    return True

def llcp_release(llc):
    summary("LLCP release")
    return True

def terminate_loop():
    global terminate_now
    return terminate_now

def main():
    clf = nfc.ContactlessFrontend()

    parser = argparse.ArgumentParser(description='nfcpy to wpa_supplicant integration for DPP NFC operations')
    parser.add_argument('-d', const=logging.DEBUG, default=logging.INFO,
                        action='store_const', dest='loglevel',
                        help='verbose debug output')
    parser.add_argument('-q', const=logging.WARNING, action='store_const',
                        dest='loglevel', help='be quiet')
    parser.add_argument('--only-one', '-1', action='store_true',
                        help='run only one operation and exit')
    parser.add_argument('--init-on-touch', '-I', action='store_true',
                        help='initiate handover on touch')
    parser.add_argument('--no-wait', action='store_true',
                        help='do not wait for tag to be removed before exiting')
    parser.add_argument('--ifname', '-i',
                        help='network interface name')
    parser.add_argument('--no-input', '-a', action='store_true',
                        help='do not use stdout input to initiate handover')
    parser.add_argument('--tag-read-only', '-t', action='store_true',
                        help='tag read only (do not allow connection handover)')
    parser.add_argument('--handover-only', action='store_true',
                        help='connection handover only (do not allow tag read)')
    parser.add_argument('--enrollee', action='store_true',
                        help='run as Enrollee-only')
    parser.add_argument('--configurator', action='store_true',
                        help='run as Configurator-only')
    parser.add_argument('--config-params', default='',
                        help='configurator parameters')
    parser.add_argument('--ctrl', default='/var/run/wpa_supplicant',
                        help='wpa_supplicant/hostapd control interface')
    parser.add_argument('--summary',
                        help='summary file for writing status updates')
    parser.add_argument('--success',
                        help='success file for writing success update')
    parser.add_argument('--device', default='usb', help='NFC device to open')
    parser.add_argument('--chan', default=None, help='channel list')
    parser.add_argument('--altchan', default=None, help='alternative channel list')
    parser.add_argument('--netrole', default=None, help='netrole for Enrollee')
    parser.add_argument('--test-uri', default=None,
                        help='test mode: initial URI')
    parser.add_argument('--test-alt-uri', default=None,
                        help='test mode: alternative URI')
    parser.add_argument('--test-sel-uri', default=None,
                        help='test mode: handover select URI')
    parser.add_argument('--test-crn', default=None,
                        help='test mode: hardcoded crn')
    parser.add_argument('command', choices=['write-nfc-uri',
                                            'write-nfc-hs'],
                        nargs='?')
    args = parser.parse_args()
    summary(args)

    global only_one
    only_one = args.only_one

    global no_wait
    no_wait = args.no_wait

    global chanlist, altchanlist, netrole, test_uri, test_alt_uri, test_sel_uri
    global test_crn
    chanlist = args.chan
    altchanlist = args.altchan
    netrole = args.netrole
    test_uri = args.test_uri
    test_alt_uri = args.test_alt_uri
    test_sel_uri = args.test_sel_uri
    if args.test_crn:
        test_crn = struct.pack('>H', int(args.test_crn))
    else:
        test_crn = None

    logging.basicConfig(level=args.loglevel)
    for l in ['nfc.clf.rcs380',
              'nfc.clf.transport',
              'nfc.clf.device',
              'nfc.clf.__init__',
              'nfc.llcp',
              'nfc.handover']:
        log = logging.getLogger(l)
        log.setLevel(args.loglevel)

    global init_on_touch
    init_on_touch = args.init_on_touch

    global enrollee_only
    enrollee_only = args.enrollee

    global configurator_only
    configurator_only = args.configurator

    global config_params
    config_params = args.config_params

    if args.ifname:
        global ifname
        ifname = args.ifname
        summary("Selected ifname " + ifname)

    if args.ctrl:
        global wpas_ctrl
        wpas_ctrl = args.ctrl

    if args.summary:
        global summary_file
        summary_file = args.summary

    if args.success:
        global success_file
        success_file = args.success

    if args.no_input:
        global no_input
        no_input = True

    clf = nfc.ContactlessFrontend()
    global wait_connection

    try:
        if not clf.open(args.device):
            summary("Could not open connection with an NFC device", color=C_RED)
            raise SystemExit(1)

        if args.command == "write-nfc-uri":
            write_nfc_uri(clf, wait_remove=not args.no_wait)
            if not operation_success:
                raise SystemExit(1)
            raise SystemExit

        if args.command == "write-nfc-hs":
            write_nfc_hs(clf, wait_remove=not args.no_wait)
            if not operation_success:
                raise SystemExit(1)
            raise SystemExit

        global continue_loop
        while continue_loop:
            global in_raw_mode
            was_in_raw_mode = in_raw_mode
            clear_raw_mode()
            if was_in_raw_mode:
                print("\r")
            if args.handover_only:
                summary("Waiting a peer to be touched", color=C_MAGENTA)
            elif args.tag_read_only:
                summary("Waiting for a tag to be touched", color=C_BLUE)
            else:
                summary("Waiting for a tag or peer to be touched",
                        color=C_GREEN)
            wait_connection = True
            try:
                if args.tag_read_only:
                    if not clf.connect(rdwr={'on-connect': rdwr_connected}):
                        break
                elif args.handover_only:
                    if not clf.connect(llcp={'on-startup': llcp_startup,
                                             'on-connect': llcp_connected,
                                             'on-release': llcp_release},
                                       terminate=terminate_loop):
                        break
                else:
                    if not clf.connect(rdwr={'on-connect': rdwr_connected},
                                       llcp={'on-startup': llcp_startup,
                                             'on-connect': llcp_connected,
                                             'on-release': llcp_release},
                                       terminate=terminate_loop):
                        break
            except Exception as e:
                summary("clf.connect failed: " + str(e))
                break

            global srv
            if only_one and srv and srv.success:
                raise SystemExit

    except KeyboardInterrupt:
        raise SystemExit
    finally:
        clf.close()

    raise SystemExit

if __name__ == '__main__':
    main()
