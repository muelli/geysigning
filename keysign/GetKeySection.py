#!/usr/bin/env python
#    Copyright 2014 Andrei Macavei <andrei.macavei89@gmail.com>
#    Copyright 2014, 2015, 2016 Tobias Mueller <muelli@cryptobitch.de>
#
#    This file is part of GNOME Keysign.
#
#    GNOME Keysign is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    GNOME Keysign is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with GNOME Keysign.  If not, see <http://www.gnu.org/licenses/>.

import logging
from urlparse import urlparse, parse_qs, ParseResult

import requests
from requests.exceptions import ConnectionError

from .compat import gtkbutton
from .SignPages import ScanFingerprintPage, SignKeyPage, PostSignPage
from .util import mac_verify
from .util import sign_keydata_and_send as _sign_keydata_and_send

from gi.repository import Gst, Gtk, GLib
# Because of https://bugzilla.gnome.org/show_bug.cgi?id=698005
from gi.repository import GdkX11
# Needed for window.get_xid(), xvimagesink.set_window_handle(), respectively:
from gi.repository import GstVideo



Gst.init([])

FPR_PREFIX = "OPENPGP4FPR:"
progress_bar_text = ["Step 1: Scan QR Code or type fingerprint and click on 'Download' button",
                     "Step 2: Compare the received fpr with the owner's fpr and click 'Sign'",
                     "Step 3: Key was succesfully signed and an email was sent to the owner."]


# FIXME: This probably wants to go somewhere more central.
# Maybe even into Monkeysign.
log = logging.getLogger(__name__)


from .gpgmh import openpgpkey_from_data, fingerprint_from_keydata




class GetKeySection(Gtk.VBox):

    def __init__(self, app):
        '''Initialises the section which lets the user
        start signing a key.

        ``app'' should be the "app" itself. The place
        which holds global app data, especially the discovered
        clients on the network.
        '''
        super(GetKeySection, self).__init__()

        self.app = app
        self.log = logging.getLogger(__name__)

        self.scanPage = ScanFingerprintPage()
        self.signPage = SignKeyPage()
        # set up notebook container
        self.notebook = Gtk.Notebook()
        self.notebook.append_page(self.scanPage, None)
        self.notebook.append_page(self.signPage, None)
        self.notebook.append_page(PostSignPage(), None)
        self.notebook.set_show_tabs(False)
        self.notebook.connect('switch_page', self.switch_page)

        # set up the progress bar
        self.progressBar = Gtk.ProgressBar()
        self.progressBar.set_text(progress_bar_text[0])
        self.progressBar.set_show_text(True)
        self.progressBar.set_fraction(1.0/3)

        self.nextButton = Gtk.Button('Next')
        self.nextButton.connect('clicked', self.on_button_clicked)
        self.nextButton.set_image(Gtk.Image.new_from_icon_name("go-next", Gtk.IconSize.BUTTON))
        self.nextButton.set_always_show_image(True)

        self.backButton = Gtk.Button('Back')
        self.backButton.connect('clicked', self.on_button_clicked)
        self.backButton.set_image(Gtk.Image.new_from_icon_name('go-previous', Gtk.IconSize.BUTTON))
        self.backButton.set_always_show_image(True)

        bottomBox = Gtk.HBox()
        bottomBox.pack_start(self.progressBar, True, True, 0)
        bottomBox.pack_start(self.backButton, False, False, 0)
        bottomBox.pack_start(self.nextButton, False, False, 0)

        self.pack_start(self.notebook, True, True, 0)
        self.pack_start(bottomBox, False, False, 0)

        # We *could* overwrite the on_barcode function, but
        # let's rather go with a GObject signal
        #self.scanFrame.on_barcode = self.on_barcode
        self.scanPage.scanFrame.connect('barcode', self.on_barcode)
        #GLib.idle_add(        self.scanFrame.run)

        # A list holding references to temporary files which should probably
        # be cleaned up on exit...
        self.tmpfiles = []

    def switch_page(self, notebook, page, page_num):
        if page_num == 0:
            self.backButton.set_sensitive(False)
            self.nextButton.set_sensitive(True)
        elif page_num == 2:
            self.nextButton.set_sensitive(False)
            self.backButton.set_sensitive(True)
        elif page_num > 0 and page_num < 2:
            self.backButton.set_sensitive(True)
            self.nextButton.set_sensitive(True)

    def set_progress_bar(self):
        page_index = self.notebook.get_current_page()
        self.progressBar.set_text(progress_bar_text[page_index])
        self.progressBar.set_fraction((page_index+1)/3.0)


    def strip_fingerprint(self, input_string):
        '''Strips a fingerprint of any whitespaces and returns
        a clean version. It also drops the "OPENPGP4FPR:" prefix
        from the scanned QR-encoded fingerprints'''
        # The split removes the whitespaces in the string
        cleaned = ''.join(input_string.split())

        if cleaned.upper().startswith(FPR_PREFIX.upper()):
            cleaned = cleaned[len(FPR_PREFIX):]

        self.log.warning('Cleaned fingerprint to %s', cleaned)
        return cleaned


    def parse_barcode(self, barcode_string):
        """Parses information contained in a barcode

        It returns a dict with the parsed attributes.
        We expect the dict to contain at least a 'fingerprint'
        entry. Others might be added in the future.
        """
        # The string, currently, is of the form
        # openpgp4fpr:foobar?baz=qux#frag=val
        # Which urlparse handles perfectly fine.
        p = urlparse(barcode_string)
        self.log.debug("Parsed %r into %r", barcode_string, p)
        fpr = p.path
        query = parse_qs(p.query)
        fragments = parse_qs(p.fragment)
        rest = {}
        rest.update(query)
        rest.update(fragments)
        # We should probably ensure that we have only one
        # item for each parameter and flatten them accordingly.
        rest['fingerprint'] = fpr

        self.log.debug('Parsed barcode into %r', rest)
        return rest


    def on_barcode(self, sender, barcode, message, image):
        '''This is connected to the "barcode" signal.
        
        The function will advance the application if a reasonable
        barcode has been provided.
        
        Sender is the emitter of the signal and should be the scanning
        widget.
        
        Barcode is the actual barcode that got decoded.
        
        The message argument is a GStreamer message that created
        the barcode.
        
        When image is set, it should be the frame as pixbuf that
        caused a barcode to be decoded.
        '''
        self.log.info("Barcode signal %r %r", barcode, message)
        parsed = self.parse_barcode(barcode)
        fingerprint = parsed['fingerprint']
        if not fingerprint:
            self.log.error("Expected fingerprint in %r to evaluate to True, "
                           "but is %r", parsed, fingerprint)
        else:
            self.on_button_clicked(self.nextButton,
                fingerprint, message, image, parsed_barcode=parsed)


    def download_key_http(self, address, port):
        url = ParseResult(
            scheme='http',
            # This seems to work well enough with both IPv6 and IPv4
            netloc="[[%s]]:%d" % (address, port),
            path='/',
            params='',
            query='',
            fragment='')
        self.log.debug("Starting HTTP request")
        data = requests.get(url.geturl(), timeout=5).content
        self.log.debug("finished downloading %d bytes", len(data))
        return data

    def try_download_keys(self, clients):
        for client in clients:
            self.log.debug("Getting key from client %s", client)
            name, address, port, fpr = client
            try:
                keydata = self.download_key_http(address, port)
                yield keydata
            except ConnectionError as e:
                # FIXME : We probably have other errors to catch
                self.log.exception("While downloading key from %s %i",
                                    address, port)

    def verify_downloaded_key(self, downloaded_data, fingerprint, mac=None):
        log.info("Verifying key %r with mac %r", fingerprint, mac)
        if mac:
            result = mac_verify(fingerprint, downloaded_data, mac)
        else:
            try:
                imported_key_fpr = fingerprint_from_keydata(downloaded_data)
            except ValueError:
                self.log.exception("Failed to import downloaded data")
                result = False
            else:
                if imported_key_fpr == fingerprint:
                    result = True
                else:
                    self.log.info("Key does not have equal fp: %s != %s", imported_key_fpr, fingerprint)
                    result = False

        self.log.debug("Trying to validate %s against %s: %s", downloaded_data, fingerprint, result)
        return result

    def sort_clients(self, clients, selected_client_fpr):
        key = lambda client: client[3]==selected_client_fpr
        sorted_clients = sorted(clients, key=key, reverse=True)
        self.log.info("Check if list is sorted '%s'", sorted_clients)
        return sorted_clients

    def obtain_key_async(self, fingerprint, callback=None, data=None, mac=None, error_cb=None):
        self.log.debug("Obtaining key %r with mac %r", fingerprint, mac)
        other_clients = self.app.discovered_services
        self.log.debug("The clients found on the network: %s", other_clients)

        other_clients = self.sort_clients(other_clients, fingerprint)

        for keydata in self.try_download_keys(other_clients):
            if self.verify_downloaded_key(keydata, fingerprint, mac):
                is_valid = True
            else:
                is_valid = False

            if is_valid:
                # FIXME: make it to exit the entire process of signing
                # if fingerprint was different ?
                break
        else:
            self.log.error("Could not find fingerprint %s " +\
                           "with the available clients (%s)",
                           fingerprint, other_clients)
            self.log.debug("Calling error callback, if available: %s",
                            error_cb)

            if error_cb:
                GLib.idle_add(error_cb, data)
            # FIXME : don't return here
            return

        self.log.debug('Adding %s as callback', callback)
        GLib.idle_add(callback, fingerprint, keydata, data)

        # If this function is added itself via idle_add, then idle_add will
        # keep adding this function to the loop until this func ret False
        return False



    def sign_keydata_and_send(self, keydata, callback=None):
        """This is a thin (GLib) wrapper around _sign_keydata_and_send
        
        it only returns False to make GLib not constantly add this function
        to the main loop. It also saves the TemporaryFiles created
        during the signature creation process so that the MUA
        can pick them up and s.t. they will be deleted on close.
        """
        self.tmpfiles = list(_sign_keydata_and_send(keydata, error_cb=self.on_sign_error))
        return False

    def on_sign_error(self, prompt):
        self.log.error("Error signing key: %r. Trying to continue", prompt)

    def send_email(self, fingerprint, *data):
        self.log.exception("Sending email... NOT")
        return False


    def on_button_clicked(self, button, *args, **kwargs):

        if button == self.nextButton:
            self.notebook.next_page()
            self.set_progress_bar()

            page_index = self.notebook.get_current_page()
            if page_index == 1:
                if args:
                    # If we call on_button_clicked() from on_barcode()
                    # then we get extra arguments
                    fingerprint = args[0]
                    message = args[1]
                    image = args[2]
                else:
                    image = None
                    raw_text = self.scanPage.get_text_from_textview()
                    fingerprint = self.strip_fingerprint(raw_text)

                    if fingerprint == None:
                        self.log.error("The fingerprint typed was wrong."
                        " Please re-check : {}".format(raw_text))
                        # FIXME: make it to stop switch the page if this happens
                        return

                # save a reference to the last received fingerprint
                self.last_received_fingerprint = fingerprint
                
                # Okay, this is weird.  If I don't copy() here,
                # the GstSample will get invalid.  As if it is
                # free()d although I keep a reference here.
                self.scanned_image = image.copy() if image else None

                # We also may have received a parsed_barcode" argument
                # with more information about the key to be retrieved
                barcode_information = kwargs.get("parsed_barcode", {})
                # FIXME: This is a hack while the list is not flattened
                mac = barcode_information.get('MAC', [None])[0]
                self.log.info("Transferred MAC via barcode: %r", mac)

                # error callback function
                err = lambda x: self.signPage.mainLabel.set_markup('<span size="15000">'
                        'Error downloading key with fpr\n{}</span>'
                        .format(fingerprint))
                # use GLib.idle_add to use a separate thread for the downloading of
                # the keydata.
                # Note that idle_add does not seem to take kwargs...
                # So we work around by cosntructing an anonymous function
                GLib.idle_add(lambda: self.obtain_key_async(fingerprint, self.recieved_key,
                        fingerprint, mac=mac, error_cb=err))


            if page_index == 2:
                # self.received_key_data will be set by the callback of the
                # obtain_key function. At least it should...
                # The data flow isn't very nice. It probably needs to be redone...
                f = lambda: self.sign_keydata_and_send(
                                    keydata=self.received_key_data,
                                    callback=self.send_email)
                GLib.idle_add(f)


        elif button == self.backButton:
            self.notebook.prev_page()
            self.set_progress_bar()


    def recieved_key(self, fingerprint, keydata, *data):
        self.received_key_data = keydata
        image = self.scanned_image
        openpgpkey = openpgpkey_from_data(keydata)
        assert openpgpkey.fingerprint == fingerprint
        self.signPage.display_downloaded_key(openpgpkey, fingerprint, image)
