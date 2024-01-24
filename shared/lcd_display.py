# (c) Copyright 2023 by Coinkite Inc. This file is covered by license found in COPYING-CC.
#
# lcd_display.py - LCD rendering for Q1's 320x240 pixel *colour* display!
#
import machine, uzlib, utime, array
from uasyncio import sleep_ms
from graphics_q1 import Graphics
from st7788 import ST7788
from utils import xfp2str
from ucollections import namedtuple

# the one font: fixed-width (except for a few double-width chars)
from font_iosevka import CELL_W, CELL_H, TEXT_PALETTES, COL_TEXT, COL_DARK_TEXT, COL_SCROLL_DARK
from font_iosevka import FontIosevka

#WIDTH = const(320)
#HEIGHT = const(240)
LEFT_MARGIN = const(7)      # equal on right side, but used for scroll bar
TOP_MARGIN = const(15)
PROGRESS_BAR_H = const(5)
ACTIVE_H = const(240 - TOP_MARGIN - PROGRESS_BAR_H)
CHARS_W = const(34)
CHARS_H = const(10)

# colouuurs: RGB565
COL_WHITE = 0xffff
COL_BLACK = 0x0000
COL_PROGRESS = COL_TEXT

# Attribute stored per-char; really just an index into TEXT_PALETTES
FLAG_INVERT = 0x10000
FLAG_DARK   = 0x20000
ATTR_MASK   = 0x30000

# use this to describe cursor you need.
# - outline leaves most of the cell unaffected (just 1px inside border)
# - solid/outline available in double-width as well
CURSOR_SOLID = 0x01
CURSOR_OUTLINE = 0x02
CURSOR_MENU = 0x03
CURSOR_DW_OUTLINE = 0x11
CURSOR_DW_SOLID = 0x12

CursorSpec = namedtuple('CursorSpec', 'x y cur_type')
CURSOR_DW_Mask = 0x10

def grey_level(amt):
    # give percent 0..1.0
    r = int(amt * 0x1f)
    g = int(amt * 0x3f)
    #b = int(amt * 0x1f)        # same as Red

    return (r<<11) | (g << 5) | r

def rgb(r,g,b):
    # as if 24-bit, but we're 16
    r = int(r/255 * 0x1f)
    g = int(g/255 * 0x3f)
    b = int(b/255 * 0x1f)
    return (r<<11) | (g << 5) | b


def get_sys_status():
    # Read current values for all status-bar items
    # - normally we update as we go along.
    # - return a dict
    from battery import get_batt_threshold

    rv = dict(shift=0, caps=0, symbol=0)
    rv['bat'] = get_batt_threshold()

    from stash import bip39_passphrase
    rv['bip39'] = int(bool(bip39_passphrase))

    from pincodes import pa
    rv['tmp'] = int(bool(pa.tmp_value))

    from glob import settings
    if settings:
        rv['xfp'] = settings.get('xfp')

    from version import is_edge, is_devmode
    if is_edge:
        rv['edge'] = 1
    elif is_devmode:
        rv['devmode'] = 1

    return rv


class Display:

    # XXX move  to global, but rest of system looks at these member vars
    WIDTH = 320
    HEIGHT = 240

    # use these negative X values for auto layout features
    CENTER = -2
    RJUST = -1

    # use this to know if on Q1 or earlier 
    has_lcd = True

    # icon names and their values (0 / 1)
    status_icons = {}

    def __init__(self):
        self.dis = ST7788()

        from gpu import GPUAccess
        self.gpu = GPUAccess()
        try:
            self.gpu.upgrade_if_needed()
        except:
            print("GPU upgrade failed")

        self.last_buf = self.make_buf(0)
        self.next_buf = self.make_buf(32)

        # state of progress bar (bottom edge)
        self.last_prog_x = -1
        self.last_prog_w = -1
        self.next_prog_x = 0
        self.next_prog_w = 0

        # state of scroll bar (right side)
        self.last_scroll = 0.0
        self.next_scroll = None

        self.last_bar_update = 0
        #self.dis.fill_screen()     # defer a bit
        self.draw_status(full=True)

    def make_buf(self, ch=32):
        # make a screen-state storage buffer. One spot per character, but needs to
        # store attributes as well as support 16-bit unicode
        return [array.array('I', (ch for i in range(CHARS_W))) for y in range(CHARS_H)]

    def redraw_metakeys(self, new_state):
        # called when metakeys have changed state
        self.draw_status(**new_state)

    async def async_draw_status(self, **kws):
        self.draw_status(**kws)

    def set_lcd_brightness(self, on_battery=None, tmp_override=None):
        # Call when battery changes state, or if you want max for a bit (QR display)
        # - call w/o args to get back to state we're supposed to be in.
        from glob import settings
        from battery import get_batt_threshold, DEFAULT_BATT_BRIGHTNESS

        if tmp_override is not None:
            self.dis.backlight.intensity(tmp_override)
            return

        # otherwise: respect setting

        if on_battery is None:
            on_battery = (get_batt_threshold() != None)

        if on_battery:
            # user-defined brightness when running on batteries.
            lvl = DEFAULT_BATT_BRIGHTNESS
            if settings:
                lvl = settings.get('bright', DEFAULT_BATT_BRIGHTNESS)
            self.dis.backlight.intensity(lvl)
        else:
            # full brightness when on VBUS and when showing QR's
            self.dis.backlight.intensity(255)

    def draw_status(self, full=False, **kws):
        self.gpu.take_spi()

        if full:
            y = TOP_MARGIN
            self.dis.fill_rect(0, 0, WIDTH, y-1, 0x0)
            self.dis.fill_rect(0, y-1, WIDTH, 1, grey_level(0.25))
            kws = get_sys_status()


        b_x = 290
        if 'bat' in kws:
            if kws['bat'] is None:
                self.image(b_x, 0, 'plugged')
                self.set_lcd_brightness(False)
            else:
                self.image(b_x, 0, 'bat_%d' % kws['bat'])
                self.set_lcd_brightness(True)

        if 'bip39' in kws:
            self.image(102, 0, 'bip39_%d' % kws['bip39'])

        if 'tmp' in kws:
            self.image(165, 0, 'tmp_%d' % kws['tmp'])

        xfp = kws.get('xfp', None)      # expects an integer
        if xfp != None:
            x = 215
            for ch in xfp2str(xfp).lower():
                self.image(x, 0, 'ch_'+ch)
                x += 6

        x = 265
        if 'edge' in kws:
            self.image(x, 0, 'edge')
        elif 'devmode' in kws:
            self.image(x+5, 0, 'devmode')

        x = 8
        for dx, meta in [(7, 'shift'), (37, 'symbol'), (58, 'caps')]:
            if meta in kws:
                self.image(x+dx, 0, '%s_%d' % (meta, kws[meta]))

    def image(self, x, y, name):
        # display a graphics image, immediately
        w,h, data = getattr(Graphics, name)
        if x is None:
            x = max(0, (WIDTH - w) // 2)
        self.gpu.take_spi()
        self.dis.show_zpixels(x, y, w, h, data)
        self.mark_correct(x, y, w, h)
        self.show()

    def mark_correct(self, px, py, w, h):
        # mark a subset of the screen as already drawn correctly
        # - because we drew an image in that spot already (immediate)
        # - hard: need to convert from pixel coord space to chars
        if py < TOP_MARGIN:
            # status icons not a concern
            return

        cy = (py - TOP_MARGIN) // CELL_H
        cx = (px - LEFT_MARGIN) // CELL_W
        cw = (w+CELL_W) // CELL_W
        ch = (h+CELL_H) // CELL_H
        #print('pixel %dx%d @ (%d,%d) => %dx%d @ (%d,%d)' % (w, h, px,py,  cw, ch, cx,cy))

        for y in range(cy, cy+ch+1):
            for x in range(cx, cx+cw+1):
                try:
                    self.last_buf[y][x] = self.next_buf[y][x] = 0xfffe
                except IndexError:
                    pass

    def icon(self, x, y, name, invert=0):
        # plan is these are chars or images
        raise NotImplementedError

    def width(self, msg):
        # length of text msg in char cells
        # - typically 1:1 but we have a few double-width chars
        rv = len(msg)
        rv += sum(1 for ch in msg if ch in FontIosevka.DOUBLE_WIDE)
        return rv

    def text(self, x,y, msg, font=None, invert=False, dark=False):
        # Draw at x,y (in cell positions, not pixels)
        # - use invert=1 to get reverse video
        # - returns ending X position, where you might want a cursor after
        end_x = None

        # encode text attribute for this part
        attr = 0
        if invert:
            attr = FLAG_INVERT
        if dark:
            attr = FLAG_DARK

        if x is None or x < 0:
            w = self.width(msg)
            if x == None:
                # center: also blanks rest of line
                x = max(0, (CHARS_W - w) // 2)
                end_x = x + w
                msg = ((' '*x) + msg + (' ' * CHARS_W))[0:CHARS_W]
                x = 0
            else:
                # measure from right edge (right justify)
                x = max(0, CHARS_W - w + 1 + x)
                end_x = x + w

        if y < 0:
            # measure up from bottom edge
            y = CHARS_H + y

        if y >= CHARS_H: 
            #print("BAD Draw '%s' at y=%d" % (msg, y))
            return     # past bottom

        for ch in msg:
            if x >= CHARS_W: break
            self.next_buf[y][x] = ord(ch) + attr
            x += 1
            if ch in FontIosevka.DOUBLE_WIDE:
                if x >= CHARS_W: break              # XXX will that look right?
                self.next_buf[y][x] = 0
                x += 1

        return end_x if end_x is not None else x

    def real_clear(self, _internal=False):
        # fill to black, but only text area, not status bar
        if not _internal:
            self.gpu.take_spi()
            self.dis.fill_rect(0, TOP_MARGIN, WIDTH, HEIGHT-TOP_MARGIN, 0x0)
        self.last_buf = self.make_buf(32)
        self.next_buf = self.make_buf(32)
        self.next_prog_w = 0
        self.next_scroll = None

    def clear(self):
        # clear text
        self.next_buf = self.make_buf(32)
        # clear progress bar / scroll
        self.next_prog_w = 0
        self.next_scroll = None

    def show(self, just_lines=None, cursor=None, max_bright=False):
        # Push internal screen representation to device, effeciently
        self.gpu.take_spi()

        lines = just_lines or range(CHARS_H)
        for y in lines:
            x = 0
            while x < CHARS_W:
                if self.next_buf[y][x] == self.last_buf[y][x]:
                    # already correct
                    x += 1
                    continue

                py = TOP_MARGIN + (y * CELL_H)
                px = LEFT_MARGIN + (x * CELL_W)
                ch = chr(self.next_buf[y][x] & ~ATTR_MASK)
                attr = (self.next_buf[y][x] & ATTR_MASK)

                if ch == ' ':
                    # space - look for horz runs & fill w/ blank
                    run = 1
                    for x2 in range(x+1, CHARS_W):
                        if self.next_buf[y][x] != self.next_buf[y][x2]:
                            break                                        
                        run += 1

                    self.dis.fill_rect(px, py, run*CELL_W, CELL_H, 
                                COL_TEXT if attr == FLAG_INVERT else 0)
                    x += run
                    continue

                fn = FontIosevka.lookup(ch)
                if not fn:
                    # unknown char
                    x += 1
                    continue

                self.dis.show_pal_pixels(px, py, fn.w, fn.h, TEXT_PALETTES[attr >> 16], fn.bits)

                x += fn.w // CELL_W

            self.last_buf[y][:] = self.next_buf[y]

        # maybe update progress bar
        if (self.next_prog_x, self.next_prog_w) != (self.last_prog_x, self.last_prog_w):
            # NOTE: misc/gpu/lcd.c may need update to follow future changes here
            x = self.next_prog_x
            w = self.next_prog_w
            h = PROGRESS_BAR_H
            self.dis.fill_rect(0, HEIGHT-h, WIDTH, h, COL_BLACK)
            if w:
                self.dis.fill_rect(x, HEIGHT-h, w, h, COL_PROGRESS)

            self.last_prog_x = x
            self.last_prog_w = w

        if self.next_scroll != self.last_scroll:
            self._draw_scroll_bar(self.next_scroll)
            self.last_scroll = self.next_scroll

        if cursor:
            # implement CursorSpec values
            assert 0 <= cursor.x < CHARS_W, 'cur x'
            assert 0 <= cursor.y < CHARS_H, 'cur y'
            self.gpu.cursor_at(cursor.x, cursor.y, cursor.cur_type)
            self.last_buf[cursor.y][cursor.x] = 0xfffd
            if (cursor.cur_type & CURSOR_DW_Mask) and (cursor.x < CHARS_W-1):
                self.last_buf[cursor.y][cursor.x+1] = 0xfffd

        # modulate the LCD brightness if we're showing QR or something
        if max_bright:
            self.set_lcd_brightness(tmp_override=255)
            self._max_bright = True
        elif hasattr(self, '_max_bright'):
            self.set_lcd_brightness()       # back to normal
            del self._max_bright
            

    # When drawing another screen for a bit, then coming back, use these
    def save_state(self):
        # TODO: should be a dataclass w/ all our state details
        return ([array.array('I', ln) for ln in self.last_buf],
                    self.last_prog_x, self.last_prog_w,
                    self.last_scroll)

    def restore_state(self, old_state):
        rows, self.next_prog_x, self.next_prog_w, self.next_scroll = old_state
        for y in range(CHARS_H):
            self.next_buf[y][:] = rows[y]
        self.show()

    # obsolete OLED approach
    def save(self):
        raise NotImplementedError
    def restore(self):
        raise NotImplementedError

    def hline(self, y):
        # used only in hsm_ux.py
        #self.dis.fill_rect(0,y, WIDTH, 1, 0xffff)
        pass

    def vline(self, x):
        # used only in hsm_ux.py
        #self.dis.fill_rect(x,TOP_MARGIN, 1, ACTIVE_H, 0xffff)
        pass

    def clear_rect(self, x,y, w,h):
        # but see clear_box() instead
        raise NotImplementedError

    def scroll_bar(self, offset, count, per_page=CHARS_H):
        # next show(), we will draw a scroll bar on right edge
        assert count >= 1
        self.next_scroll = (offset, count, per_page)

    def _draw_scroll_bar(self, values):
        # Immediately draw bar along right edge.
        bw = 5      # bar width
        if values is None:
            # clear old display
            self.dis.fill_rect(WIDTH-bw, TOP_MARGIN, bw, ACTIVE_H, COL_BLACK)
            return

        offset, count, per_page = values

        assert 0 <= offset <= count, (offset, count, per_page)
        num_pages = max(count / per_page, 2)
        bh = max(int(ACTIVE_H / num_pages), 4)
        pos = int((ACTIVE_H - bh) * (offset / count))

        # "round up" the final page so touches bottom always
        is_last = offset and (offset + per_page >= count)
        if is_last:
            pos = ACTIVE_H - bh

        self.dis.fill_rect(WIDTH-bw, TOP_MARGIN, bw, ACTIVE_H, COL_SCROLL_DARK)
        self.dis.fill_rect(WIDTH-bw, TOP_MARGIN+pos, bw, bh, COL_TEXT)

    def fullscreen(self, msg, percent=None):
        # show a simple message "fullscreen". 
        self.clear()
        self.text(None, CHARS_H // 3, msg)
        if percent is not None:
            self.progress_bar(percent)
        self.show()

    def splash(self):
        # display a splash screen with some version numbers
        self.real_clear()

        y = 6
        self.image(None, 90, 'splash')
        self.text(None, y, "Don't Trust. Verify.")

        from version import get_mpy_version
        timestamp, label, *_ = get_mpy_version()

        self.text(0,  -1, 'Version '+label)
        self.text(-1, -1, timestamp)
        self.show([y, CHARS_H-1])

    def progress_bar(self, percent):
        # Horizontal progress bar
        # takes 0.0 .. 1.0 as fraction of doneness
        percent = max(0, min(1.0, percent))
        self.next_prog_x = 0
        self.next_prog_w = int(WIDTH * percent)

    def progress_part_bar(self, n_of_m):
        # for BBQr: a part of a bar
        n, m = n_of_m
        if m == 1:
            self.next_prog_x = self.next_prog_w = 0
        else:
            w = WIDTH // m
            self.next_prog_x = (n * w)
            self.next_prog_w = w

    def progress_sofar(self, done, total):
        # Update progress bar, but only if it's been a while since last update
        if utime.ticks_diff(utime.ticks_ms(), self.last_bar_update) < 100:
            return
        self.last_bar_update = utime.ticks_ms()
        self.progress_bar_show(done / total)

    def progress_bar_show(self, percent):
        # useful as a callback
        self.progress_bar(percent)
        self.show()

    def mark_sensitive(self, from_y, to_y):
        # XXX maybe TODO ? or remove ... LCD doesnt have issue
        return

    def busy_bar(self, enable, speed_code=5):
        # activate the GPU to render/animate this.
        # - show() in this funct is relied-upon by callers
        if enable:
            self.last_prog_x = self.next_prog_x = -1
            self.show()
            self.gpu.busy_bar(True)
        else:
            # - self.show will stop animation
            # - and redraw w/ no bar visible
            self.last_prog_x = -1
            self.last_prog_w = -1
            self.next_prog_x = 0
            self.next_prog_w = 0
            self.show()

    def set_brightness(self, val):
        # - was only used by HSM ux code
        # - QR code display brightness is done in show_qr_data() now
        # - see self.set_lcd_brightness()
        return 

    def menu_draw(self, ry, msg, is_sel, is_checked, space_indicators):
        # draw a menu item, perhaps selected, checked.
        assert CHARS_W == 34

        if ry >= CHARS_H:
            # higher layer tries to draw partial line past bottom, and that's
            # ok because the mk4 had a 5th, half-line as a hint
            return

        if msg[0] == ' ' and space_indicators:
            # unused, but might need?
            msg = '␣' + msg[1:]

        x = 0
        self.text(x, ry, ' '+msg+' ', invert=is_sel)

        if is_checked:
            self.text(len(msg)+2, ry, '✔')

    def menu_show(self, cursor_y):
        cs = CursorSpec(0, cursor_y or 0, CURSOR_MENU)
        self.show(cursor=cs)

    def show_yikes(self, lines):
        # dump a stack trace
        # - intended for photos, sent to support!
        from utils import word_wrap

        self.clear()
        self.text(None, 0, '>>>> Yikes!! <<<<')

        y = 1
        for num, ln in enumerate(lines):
            ln = ln.strip()

            if ln[0:6] == 'File "':
                # convert: File "main.py", line 63, in interact
                #    into: main.py:63  interact
                ln = ln[6:].replace('", line ', ':').replace(', in ', '  ')
                if ln[0] == '/':
                    ln = ln.split('/')[-1]

            for second, l in enumerate(word_wrap(ln, CHARS_W)):
                self.text(1 if second else 0, y, l)
                y += 1

        self.show()

    def draw_story(self, lines, top, num_lines, is_sensitive, hint_icons=''):
        self.clear()

        y=0
        for ln in lines:
            if ln == 'EOT':
                self.text(0, y, '─'*CHARS_W, dark=True)
                continue
            elif ln and ln[0] == '\x01':
                # title ... but we have no special font? Inverse!
                self.text(0, y, ' '+ln[1:]+' ', invert=True)
                if hint_icons:
                    # maybe show that [QR] can do something
                    self.text(-1, y, hint_icons, dark=True)
            else:
                self.text(0, y, ln)

            y += 1

            if is_sensitive and len(ln) > 3 and ln[2] == ':':
                self.mark_sensitive(y, y+13)

        self.scroll_bar(top, num_lines, CHARS_H)
        self.show()

    def draw_qr_display(self, qr_data, msg, is_alnum, sidebar, idx_hint, invert, partial_bar=None):
        # Show a QR code on screen w/ some text under it
        # - invert not supported on Q1
        # - sidebar not supported here (see users.py)
        # - we need one more (white) pixel on all sides
        from utils import word_wrap

        self.real_clear()
        if partial_bar is not None:
            self.progress_part_bar(partial_bar)

        # maybe show something other than QR contents under it
        msg = sidebar or msg

        if msg:
            if len(msg) <= CHARS_W:
                parts = [msg]
            elif ' ' not in msg and (len(msg) <= CHARS_W*2):
                # fits in two lines, but has no spaces (ie. payment addr)
                # so split nicely, and shift off center
                hh = len(msg) // 2
                parts = [msg[0:hh] + '  ', '  '+msg[hh:]]
            else:
                # do word wrap
                parts = list(word_wrap(msg, CHARS_W))

            num_lines = len(parts)
        else:
            num_lines = 0

        if num_lines > 2:
            # show no text if it would be too big (example: 18, 24 seed words)
            num_lines = 0
            del parts

        # send packed pixel data to C level to decode and expand onto LCD
        # - 8-bit aligned rows of data
        scan_w, w, data = qr_data.packed() if hasattr(qr_data, 'packed') else qr_data

        # always draw as large as possible (vertical is limit)
        expand = max(1, (ACTIVE_H - (num_lines * CELL_H))  // (w+2))
        qw = (w+2) * expand

        # horz/vert center in available space
        y = (ACTIVE_H - (num_lines * CELL_H) - qw) // 2
        x = (WIDTH - qw) // 2

        self.gpu.take_spi()
        self.dis.show_qr_data(x, TOP_MARGIN + y, w, expand, scan_w, data)
        self.mark_correct(x, TOP_MARGIN + y, qw, qw)

        if num_lines:
            # centered text under that
            y = CHARS_H - num_lines
            for line in parts:
                self.text(None, y, line)
                y += 1

        if idx_hint:
            # show path index number: just 1 or 2 digits
            self.text(-1, 0, idx_hint)

        # pass a max brightness flag here, which will be cleared after next show
        self.show(max_bright=True)

    def draw_bbqr_progress(self, hdr, got_parts, corrupt=False):
        # we've seen at least one BBQr QR, so update display w/ progress bar
        # - lots of data so we can show nice animation
        # - hdr:BBQrHeader instance
        count = len(got_parts)
        if hdr.num_parts < (CHARS_W // 2):
            # if not too many parts, show - or 3 as they arrive
            pat = []
            for i in range(hdr.num_parts):
                if i in got_parts:
                    pat.append(str(i+1))
                else:
                    wl = 1 if i < 9 else 2
                    if corrupt and i == hdr.which:
                        pat.append('X'*wl)
                    else:
                        pat.append('-'*wl)

            pat = ('  ' if hdr.num_parts <= 8 else ' ').join(pat)
            if len(pat) > CHARS_W:
                pat = ''
        else:
            pat = ''                # clear line

        self.text(None, -3, pat)

        self.text(None, -2, 'Keep scanning more...' if count < hdr.num_parts else 'Got all parts!')
        self.text(None, -1, '%s: %d of %d parts' % (hdr.file_label(), count, hdr.num_parts),
                                                        dark=True)
        percent = count / hdr.num_parts
        self.progress_bar(percent)
        self.show()

    def draw_box(self, x, y, w, h, **kw):
        # using line-drawing chars, draw a box
        # returns X pos of first inside char
        assert 0 <= h <= CHARS_H-2      # 8 max
        assert 0 <= w <= CHARS_W-2      # 32 max

        if x is None:
            x = (CHARS_W - w - 2) // 2
        ln = '┏' + ('━'*w) + '┓'
        self.text(x, y, ln, **kw)
        for yy in range(y+1, y+h+1):
            self.text(x, yy,  '┃', **kw)
            self.text(x+w+1,  yy, '┇', **kw)

        ln = '┗' + ln[1:-1] + '┛'
        self.text(x, y+h+1, ln, **kw)

        return x+1

    def clear_box(self, x, y, w, h):
        # clear (w/ spaces) a box on screen
        for Y in range(y, y+h):
            for X in range(x, x+w):
                assert 0 <= X < CHARS_W, X
                assert 0 <= Y < CHARS_H, Y
                self.next_buf[Y][X] = 32

    def bootrom_takeover(self):
        # we are going to go into the bootrom and have it do stuff on the
        # screen... we need to redraw completely on return
        self.gpu.take_spi()     # blocks until xfer complete
        self.last_buf = self.make_buf(0)
        self.last_prog_x = -1

        
# here for mpy reasons
WIDTH = const(320)
HEIGHT = const(240)

# EOF
