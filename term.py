SIZE = 10
HEIGHT = 40
WIDTH  = 140

import time
import cProfile
import sys
import os
import pty
import fcntl
import termios
import struct
import threading

from collections import deque
from functools import partial
from weakref import WeakKeyDictionary
from pprint import pprint

import pyglet
from pyglet.window import key

def thread(fun):
    def s(*args, **kwargs):
        t = threading.Thread(target=lambda: fun(*args), **kwargs)
        t.start()
        return t
    return s

symbols = {
   key.BACKSPACE: "", key.UP: 	"OA",
   key.DOWN:	"OB", key.LEFT:	"OD",
   key.RIGHT:	"OC", key.HOME:	"OH",
   key.END: 	"OF", key.PAGEUP:	"[5~",
   key.PAGEDOWN:"[6~",key.F1: 	"OP",
   key.F2:	"OQ",	key.F3: 	"OR",
   key.F4:	"OS", key.F5: 	"OT",
   key.F6:	"OU", key.F7: 	"OV",
   key.F8:	"OW", key.F9: 	"OX",
   key.F10:	"OY",	key.F11:	"OZ",
   key.F12:	"[24~",key.ESCAPE:	"",
   key.TAB:	"\t",
}



application_symbols = {
    key.NUM_ENTER:"OM", key.NUM_1:	"Op",
    key.NUM_2:	"Oq", key.NUM_3:	"Or",
    key.NUM_4:	"Os", key.NUM_5:	"Ot",
    key.NUM_5:	"Ou", key.NUM_6:	"Ov",
    key.NUM_7:	"Ow", key.NUM_8:	"Ox",
    key.NUM_9:	"Oy", 
}

class DefAttr:
    def __init__(self, default=lambda obj: None, **kwargs):
        super().__init__(**kwargs)
        self.default = default

    def __get__(self, obj, type_=None):
        try:
            return super().__get__(obj, type_)
        except KeyError:
            return self.default(obj)
    
class Descriptor:
    """Allows a class to check before an attribute is retrieved,
    or to update after it has been changed.

    Also defaults
    """
    def __init__(self, onset=lambda obj: None, onget=lambda obj, val:val, default=None):
        self.onset = onset
        self.onget = onget
        self.vals = WeakKeyDictionary()
        self.default = default

    def __get__(self, obj, type_=None):
        val = self.vals.get(obj, self.default)
        return self.onget(obj, val)

    def __set__(self, obj, value):
        self.vals[obj] = value
        self.onset(obj)


class Bound(Descriptor): 

    """On top of the functionallity provided by Descriptor, Bound ensures that
    all values assigned to an attribute stay above or equal to the result of
    low, and below or equal to the result of high. Bounds start as 0 through
    inf.  
    """

    @staticmethod
    def constrain(val, low=None, high=None):
        pick = max(val, low)   if low is not None else val
        return min(pick, high) if high is not None else pick
        
    def __init__(self, high=lambda obj:None, low=lambda obj:0, **kw):
        super().__init__(**kw)
        self.high = high
        self.low = low

    def __set__(self, obj, value):
        Descriptor.__set__(self, obj, self.constrain(value, self.low(obj), self.high(obj)))
    
class Line:
    @Descriptor
    def text(self):
        self.dirty = True

    def __init__(self, text=""):
        self.dirty = True
        self.text = ""
        self.graphics = []

class Term:
    @Descriptor
    def dims(self):
        self.update_size()
        
    @property
    def width(self):
        return self.dims[1]
    @width.setter
    def set_width(self, val):
        self.dims = (self.height, val)

    @property
    def height(self):
        return self.dims[0]
    @height.setter
    def set_height(self, val):
        self.dims = (val, self.width)

    def margin_height(self):
        return self.height-1
    def margin_onset(self):
        self.row = self.row #descriptor magic
    margin_top = Bound(margin_height, onset=margin_onset)
    margin_bottom = Bound(margin_height, onset=margin_onset)
    row = Bound(high=lambda self: self.margin_top,
                low=lambda self: self.margin_bottom,
                default=0)
        
    col = Bound()

    def label(self, text, y, batch=None):
        return pyglet.text.Label(
                text,
                batch=batch,
                font_size=self.font_size,
                font_name=self.font_name,
                x=1, y=y * self.font_height + 1,
                anchor_x='left', anchor_y='bottom',
            )

    def __init__(self, width, height, font_size, shell="/bin/bash", font_name="SourceCodePro for Powerline"): 
        self.fd = self.fork_pty(shell, shell, "-i")
        self.chars = deque()

        self.font_name = font_name
        self.font_size = font_size
        self.font_height = 0#temporary
        temp_label = self.label("█", 0)
        self.font_height = temp_label.content_height
        self.font_width = temp_label.content_width

        self.lines = [""]*height
        self.dirty = set()
        self.batch = None
        self.dims = (height, width) #initialises batch

        self.margin_top = self.height - 1 #TODO: top margin should follow height
        self.margin_bottom = 0
        self.row = 0
        self.col = 0

        self.window = pyglet.window.Window(width=self.width*self.font_width, 
                                          height=self.height*self.font_height,
                                          resizable=True)
        self.window.event(self.on_draw)
        self.window.event(self.on_mouse_scroll)
        self.window.event(self.on_key_press)
        self.window.event(self.on_text)
        self.window.event(self.on_resize)

        self.saved_cursor = (self.row, self.col)

        self.modes = {
            "application": False,
            "edit": False,
            "cursor": True,
            "vertical": False,
            "insert": False,
            "autowrap": False,
            "mouse": False,
        }

        self.lock = threading.Lock()
        self.actor = self.act(self.fill(), self.lock)

    def start(self):
        pyglet.clock.schedule(self.redraw)
        pyglet.app.run()


    def on_draw(self):
        self.window.clear()
        self.draw_cursor()
        for d in self.dirty:
            if d < self.height:
                self.labels[d].text = self.lines[d]
        self.batch.draw()
        self.window.invalid = False

    def on_mouse_scroll(self, x, y, scroll_x, scroll_y):
        if not self.modes["mouse"]:
            if scroll_y < 0:
                self.write(''*-scroll_y)
            elif scroll_y > 0:
                self.write(''*scroll_y)
        else:
            if scroll_y < 0:
                self.write('\x1b[Ma%c%c'%(32+x//self.font_width,32+y//self.font_height)*-scroll_y)
            elif scroll_y > 0:
                self.write('\x1b[M`%c%c'%(32+x//self.font_width,32+y//self.font_height)*scroll_y)

    def on_key_press(self, symbol, modifiers):
        if modifiers & key.MOD_CTRL and 96 < symbol <= 122:
            self.write(chr(symbol - 96))
        elif symbol in symbols:
            self.write(symbols[symbol])
        elif self.modes["application"] and symbol in application_symbols:
            self.write(application_symbols[symbol])
        else:
            print("unknown", symbol, hex(modifiers))
            return
        return pyglet.event.EVENT_HANDLED

    def on_text(self, text):
        self.write(text)

    def on_resize(self, width, height):
        self.dims = height//self.font_height, width//self.font_width

    def redraw(self, dt):
        if not self.actor.is_alive():
            self.close()
        if self.window.invalid:
            self.lock.acquire(blocking=True)
            self.window.dispatch_event("on_draw")
            self.lock.release()

    def fork_pty(self, prog, *args):
        child_pid, fd = pty.fork()
        if child_pid == 0:
            sys.stdout.flush()
            os.execlp(prog, *args)
        else:
            return fd
    
    def write(self, item):
        os.write(self.fd, item.encode("utf-8"))

    def update_size(self):
        self.batch = pyglet.graphics.Batch()
        diff = self.height - len(self.lines)
        if diff > 0:
            self.lines += [""]*diff
        elif diff < 0:
            self.lines = self.lines[:diff]
        
        self.labels = [
            self.label(self.lines[i], i, self.batch)
            for i in range(self.height)
        ]
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, 
                    struct.pack("HHHH", self.height, self.width, 0, 0))

    def fill(self):
        data = b""
        while True:
            try:
                data += os.read(self.fd, 10000)
                try:#we might sometimes get data that ends in the middle of a
                    #unicode glyph. In that case we need to wait. However,
                    #UnicodeDecodeError can occur in many other cases. C'est la 
                    #vie. ¯\_(ツ)_/¯
                    yield from data.decode("utf-8")
                    print()
                except UnicodeDecodeError:
                    pass
                else:
                    data = b""
            except OSError:
                break

    def close(self):
        pyglet.app.exit()


    def draw_cursor(self):
        if not self.modes["cursor"]:
            return
        x, y = self.col*(self.font_width), (self.row) * (self.font_height), 
        pyglet.graphics.draw(4, pyglet.gl.GL_QUADS, 
            ('v2f', [
                x, y,
                x+self.font_width, y, 
                x+self.font_width, y+self.font_height,
                x, y+self.font_height]),
            ("c3B", [200]*12))

    def insert_line(self, index, data):
        self.lines.pop()
        self.lines.insert(index, data)
        self.dirty.update(range(index, self.height))

    def insert(self, chars):
        print(chars, end="")
        l = self.lines[self.row]
        if self.modes["insert"]:
            self.lines[self.row] = l[:self.col].ljust(self.col, " ") + chars + l[self.col:]
        else:
            self.lines[self.row] = l[:self.col].ljust(self.col, " ") + chars + l[self.col+len(chars):]
        self.col += len(chars)
        if len(l) >= self.width:
            if self.modes["autowrap"]:
                self.insert_line(0, "")
                self.col = 0
                self.insert(self.lines[self.row][self.width:])
                self.splice(self.width, None, self.row + 1)
                self.dirty.add(self.row+1)
            else:
                self.splice(self.width, None, self.row)
        self.dirty.add(self.row)

    def remove(self, index):
        self.lines.pop(index)
        self.lines.append("")
        self.dirty.update(range(index, self.height))

    def splice(self, start, end, row=None):#doesn't update col
        row = row if row is not None else self.row 
        l = self.lines[row]
        self.lines[row] = l[:start] + (l[end:] if end is not None else "")
        self.dirty.add(row)

    def csi(self, chars):
        coms = [0]
        follow = next(chars)
        query = ""
        if follow in "<=>?":
            query = follow
            follow = next(chars)
        while follow in "0123456789;":
            if follow == ";":
                coms.append(0)
            else:
                coms[-1] *= 10
                coms[-1] += int(follow)
            follow = next(chars)
        if follow not in "m":
            print("'CSI", query, coms, follow, "'", sep="", end="")
        if follow == "A":
            self.row -= coms[0] or 1
        elif follow == "B":
            self.row += coms[0] or 1
        elif follow == "C":
            self.col += coms[0] or 1
        elif follow == "D":
            self.col -= coms[0] or 1
        elif follow == "G":
            self.col = max(coms[0]-1, 0)
        elif follow == "H":
            self.row = self.height - (coms[0] or 1)
            if len(coms)>1:
                self.col = coms[1] - 1
            else:
                self.col = 0
        elif follow == "J":
            if coms[0] in (0, 2):
                self.splice(self.col, None)
                for i in range(self.row -1, -1, -1):
                    self.splice(0, None, i)
            if coms[0] in (1, 2):
                self.splice(0, self.col)
                for i in range(self.row+1, min(self.height+self.row, len(self.lines))):
                    self.splice(0, None, i)
        elif follow == "K":
            if coms[0] == 0:
                self.splice(self.col, None)
            elif coms[0] == 1:
                self.splice(0, self.col)
            elif coms[0] == 2:
                self.splice(0, None)
        elif follow == "L":#IL, insert line
            self.remove(self.margin_bottom)
            self.insert_line(self.row+1, "")
            #self.insert_line(self.row, "")
        elif follow == "M":#reMove line
            for i in range(coms[0] or 1):
                self.remove(self.row)
                self.insert_line(self.margin_bottom, "")
        elif follow == "P":
            self.splice(self.col, self.col + coms[0] if coms[0] > 0 else None)
        elif follow == "S":
            for _ in range(coms[0] or 1):   
                self.insert_line(self.margin_bottom, "")
        elif follow == "T":
            for _ in range(coms[0] or 1):
                self.remove(self.margin_bottom)
                self.insert_line(self.margin_top, "")
        elif follow == "X":
            amount = coms[0] or 1
            self.splice(self.col, self.col + amount)
            self.insert(" " * amount)
        elif follow == "Z": #back one tab
            self.col //= 8
            self.col -= coms[0]
            self.col *= 8
        elif follow == "d":
            self.row = self.height - coms[0]
            pass
        elif follow == "c" and query == ">":#secondary DA
            pass#self.write("\x1b[>0;136;0c") # what putty does 
            #https://github.com/FauxFaux/PuTTYTray/blob/1c88744f64405fbc024a15712969b083be4bc72c/terminal.c#L3548
        elif follow in "lh":
            if follow == "l":
                state = False
            elif follow == "h":
                state = True

            if coms[0] == 4 and query == "":
                self.modes["insert"] = state
            if coms[0] == 7 and query == "": #VEN
                self.modes["vertical"] = state
            elif coms[0] == 7 and query == "?":
                self.modes["autowrap"] = state
            if coms[0] == 25:#DECTCEM on
                self.modes["cursor"] = state
            elif coms[0] == 1000:#xterm mouse 1
                self.modes["mouse"] = state
            elif coms[0] == 1049:#local edit mode
                self.modes["edit"] = state
        elif follow == "m": #graphics? more like giraffics!
            pass
        elif follow == "n":
            if coms[0] == 6:
                self.write("\x1b[{};{}R".format(self.height - self.row, self.col + 1))
        elif follow == "r":#set margins
            self.margin_bottom, self.margin_top = self.height - coms[1], self.height - coms[0]
        
    @thread
    def act(self, chars, lock):
        for char in chars:
            lock.acquire(blocking=True)
            if char == "\n":
                if self.row == self.margin_bottom:
                    self.insert_line(self.row, "")
                else:
                    self.row -= 1
                self.col = 0
            elif char == "\r":
                self.col = 0
            elif char == "":
                self.col -= 1
            elif char == "":
                print("")
            elif char == "\t":
                self.insert(" " * (8 - self.col % 8))
            elif char == "\x1b":    
                follow = next(chars)
                if follow == "[": #CSI
                    self.csi(chars)
                elif follow == "(":
                    ("ESC", "(", next(chars))
                elif follow == ")":
                    ("ESC", ")", next(chars))
                elif follow == "]": #OSC
                    coms = [""]
                    for char in chars:
                        if char == "":
                            break
                        elif char == ";":
                            coms.append("")
                        else:
                            coms[-1] += char
                    if coms[0] == "0":
                        self.window.set_caption(coms[1])
                elif follow == "=":#application mode
                    self.modes["application"] = True
                elif follow == ">":#application mode
                    self.modes["application"] = False
                elif follow == "M": #reverse line feed
                    self.remove(self.margin_bottom)#just like IL
                elif follow == "7":
                    self.saved_cursor = (self.row, self.col)
                elif follow == "8":
                    self.row, self.col = self.saved_cursor
                else:
                    print("^[", follow)
                    self.insert("\x1b" + follow)
                    continue
            else:
                self.insert(char)
            self.window.invalid = True
            self.lock.release()

        
term = Term(WIDTH, HEIGHT, SIZE)


def format(line):
    fin = ""
    for ch in line:
        if ord(ch) < 32:
            fin += "^" + chr(ord(ch) + 64)
        else:
            fin += ch
    return fin

#cProfile.run("term.start()")
term.start()

