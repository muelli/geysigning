#!/usr/bin/env python
#    Copyright 2014 Tobias Mueller <muelli@cryptobitch.de>
#    Copyright 2014 Andrei Macavei <andrei.macavei89@gmail.com>
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

import argparse
import logging
import signal
import sys

from gi.repository import GObject
from gi.repository import Gst
from gi.repository import Gtk, GLib
# Because of https://bugzilla.gnome.org/show_bug.cgi?id=698005
from gi.repository import Gtk, GdkX11, GdkPixbuf
# Needed for window.get_xid(), xvimagesink.set_window_handle(), respectively:
from gi.repository import GdkX11, GstVideo

log = logging.getLogger()





class BarcodeReader(object):

    def on_barcode(self, barcode, message, image):
        '''This is called when a barcode is available
        with barcode being the decoded barcode.
        Message is the GStreamer message containing
        the barcode.'''
        return barcode

    def on_message(self, bus, message):
        log.debug("Message: %s", message)
        if message:
            struct = message.get_structure()
            struct_name = struct.get_name()
            log.debug('Message name: %s', struct_name)
            if struct_name == 'barcode':
                if struct.has_field ("frame"):
                    sample = struct.get_value ("frame")
                    log.info ("uuhh,  found image %s", sample)
                    
                    target_caps = Gst.Caps.from_string('video/x-raw,format=RGB')
                    converted_sample = GstVideo.video_convert_sample(
                        sample, target_caps, Gst.CLOCK_TIME_NONE)
                    buffer = converted_sample.get_buffer()
                    pixbuf = buffer.get_data()
                    
                assert struct.has_field('symbol')
                barcode = struct.get_string('symbol')
                log.info("Read Barcode: {}".format(barcode)) 

                timestamp = struct.get_clock_time("timestamp")[1]
                log.info("At %s", timestamp)
                
                # We now try to get the frame which caused
                # zbar to generate the barcode signal.
                # This is only an approximation, though,
                # as several threads are involved and
                # the imagesink might have advanced.
                # So this must be regarded as prototype.
                # There is https://bugzilla.gnome.org/show_bug.cgi?id=747557
                sample = self.imagesink.get_last_sample()
                log.info('last sample: %s', sample)
                caps = Gst.Caps.from_string("video/x-raw,format=RGB")
                conv = GstVideo.video_convert_sample(sample, caps, Gst.CLOCK_TIME_NONE)
                #log.debug('last data: %r', conv)
                buf = conv.get_buffer()
                image = buf.extract_dup(0, buf.get_size())

               self.on_barcode(barcode, message, image)
                

    def run(self):
        p = "v4l2src ! tee name=t ! queue ! videoconvert "
        p += " ! identity name=ident signal-handoffs=true"
        p += " ! zbar "
        p += " ! fakesink t. ! queue ! videoconvert "
        p += " ! xvimagesink name=imagesink"
        #p += " ! gdkpixbufsink"
        #p = 'uridecodebin file:///tmp/image.jpg ! tee name=t ! queue ! videoconvert ! zbar ! fakesink t. ! queue ! videoconvert ! xvimagesink'
        self.a = a = Gst.parse_launch(p)
        self.bus = bus = a.get_bus()
        self.imagesink = self.a.get_by_name('imagesink')
        self.ident = self.a.get_by_name('ident')

        bus.connect('message', self.on_message)
        bus.connect('sync-message::element', self.on_sync_message)
        bus.add_signal_watch()
        
        self.ident.connect('handoff', self.on_handoff)

        a.set_state(Gst.State.PLAYING)
        self.running = True
        while self.running and False:
            pass
        #a.set_state(Gst.State.NULL)

    def on_sync_message(self, bus, message):
        log.debug("Sync Message!")
        pass

    
    def on_handoff(self, element, buffer, *args):
        log.debug('Handing of %r', buffer)
        dec_timestamp = buffer.dts
        p_timestamp = buffer.pts
        log.debug("ts: %s", p_timestamp)


class BarcodeReaderGTK(Gtk.DrawingArea, BarcodeReader):

    __gsignals__ = {
        'barcode': (GObject.SIGNAL_RUN_LAST, None,
                    (str, # The barcode string
                     Gst.Message.__gtype__, # The GStreamer message itself
                     str, # The image data containing the barcode
                    ),
                   )
    }


    def __init__(self, *args, **kwargs):
        super(BarcodeReaderGTK, self).__init__(*args, **kwargs)


    @property
    def x_window_id(self, *args, **kwargs):
        window = self.get_property('window')
        # If you have not requested a size, the window might not exist
        assert window, "Window is %s (%s), but not a window" % (window, type(window))
        self._x_window_id = xid = window.get_xid()
        return xid

    def on_message(self, bus, message):
        log.debug("Message: %s", message)
        struct = message.get_structure()
        assert struct
        name = struct.get_name()
        log.debug("Name: %s", name)
        if name == "prepare-window-handle":
            log.debug('XWindow ID')
            message.src.set_window_handle(self.x_window_id)
        else:
            return super(BarcodeReaderGTK, self).on_message(bus, message)

    def do_realize(self, *args, **kwargs):
        #super(BarcodeReaderGTK, self).do_realize(*args, **kwargs)
        # ^^^^ does not work :-\
        Gtk.DrawingArea.do_realize(self)
        #self.run()
        #self.connect('hide', self.on_hide)
        self.connect('unmap', self.on_unmap)
        self.connect('map', self.on_map)


    def on_map(self, *args, **kwargs):
        '''It seems this is called when the widget is becoming visible'''
        self.run()

    def do_unrealize(self, *args, **kwargs):
        '''This appears to be called when the app is destroyed,
        not when a tab is hidden.'''
        self.a.set_state(Gst.State.NULL)
        Gtk.DrawingArea.do_unrealize(self)


    def on_unmap(self, *args, **kwargs):
        '''Hopefully called when this widget is hidden,
        e.g. when the tab of a notebook has changed'''
        self.a.set_state(Gst.State.PAUSED)
        # Actually, we stop the thing for real
        self.a.set_state(Gst.State.NULL)


    def do_barcode(self, barcode, message, image):
        "This is called by GObject, I think"
        log.debug("Emitting a barcode signal %s, %s", barcode, message)


    def on_barcode(self, barcode, message, image):
        '''You can implement this function to
        get notified when a new barcode has been read.
        If you do, you will not get the GObject "barcode" signal
        as it is emitted from here.'''
        log.debug("About to emit barcode signal: %s", barcode)
        self.emit('barcode', barcode, message, image)



class ReaderApp(Gtk.Application):
    '''A simple application for scanning a bar code
    
    It makes use of the BarcodeReaderGTK class and connects to
    its on_barcode signal.
    
    You need to have called Gst.init() before creating a
    BarcodeReaderGTK.
    '''
    def __init__(self, *args, **kwargs):
        super(ReaderApp, self).__init__(*args, **kwargs)
        self.connect('activate', self.on_activate)

    
    def on_activate(self, data=None):
        window = Gtk.ApplicationWindow()
        window.set_title("Gtk Gst Barcode Reader")
        reader = BarcodeReaderGTK()
        reader.connect('barcode', self.on_barcode)
        window.add(reader)

        window.show_all()
        self.add_window(window)


    def on_barcode(self, reader, barcode, message, image):
        '''All we do is logging the decoded barcode'''
        logging.info('Barcode decoded: %s', barcode)



class SimpleInterface(ReaderApp):
    '''We tweak the UI of the demo ReaderApp a little'''
    def on_activate(self, *args, **kwargs):
        window = Gtk.ApplicationWindow()
        window.set_title("Simple Barcode Reader")
        window.set_default_size(400, 300)

        vbox = Gtk.Box(Gtk.Orientation.HORIZONTAL, 0)
        vbox.set_margin_top(3)
        vbox.set_margin_bottom(3)
        window.add(vbox)

        reader = BarcodeReaderGTK()
        reader.connect('barcode', self.on_barcode)
        vbox.pack_start(reader, True, True, 0)
        self.playing = False

        self.image = Gtk.Image()
        vbox.pack_end(self.image, True, True, 0)


        self.playButtonImage = Gtk.Image()
        self.playButtonImage.set_from_stock("gtk-media-play", Gtk.IconSize.BUTTON)
        self.playButton = Gtk.Button.new()
        self.playButton.add(self.playButtonImage)
        self.playButton.connect("clicked", self.playToggled)
        vbox.pack_end(self.playButton, False, False, 0)

        window.show_all()
        self.add_window(window)


    def playToggled(self, w):
        self.run()


    def on_sync_message(self, bus, message):
        if message.structure is None:
            return
        if message.structure.get_name() == 'prepare-window-handle':
            #self.videoslot.set_sink(message.src)
            message.src.set_window_handle(self.xid)


    def on_message(self, bus, message):
        log.debug("Message: %s", message)
        struct = message.get_structure()
        assert struct
        name = struct.get_name()
        log.debug("Name: %s", name)
        if name == "prepare-window-handle":
            log.debug('XWindow ID')
            #self.videoslot.set_sink(message.src)
            message.src.set_window_handle(self.xid)
        else:
            return super(SimpleInterface, self).on_message(bus, message)


    def on_barcode(self, reader, barcode, message, image):
        colorspace = GdkPixbuf.Colorspace.RGB
        alpha = False
        bps = 8
        width = 800
        height = 600
        rowstride = 30
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new_take(image),
            colorspace, alpha, bps, width, height, rowstride)
        self.image.set_from_pixbuf(pixbuf)


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG,
                        format='%(name)s (%(levelname)s): %(message)s')

    # We need to have GStreamer initialised before creating a BarcodeReader
    Gst.init(sys.argv)
    app = SimpleInterface()

    try:
        # Exit the mainloop if Ctrl+C is pressed in the terminal.
        GLib.unix_signal_add_full(GLib.PRIORITY_HIGH, signal.SIGINT, lambda *args : app.quit(), None)
    except AttributeError:
        # Whatever, it is only to enable Ctrl+C anyways
        pass

    app.run()


if __name__ == '__main__':
    main()
