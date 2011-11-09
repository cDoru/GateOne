# -*- coding: utf-8 -*-
#
#       Copyright 2011 Liftoff Software Corporation
#

# TODO: See if we can spin off termio.py into its own little program that sits between Gate One and ssh_connect.py.  That way we can take advantage of multiple cores/processors (for terminal-to-HTML processing).  There's no reason why we can't write something that does what dtach does.  Just need to redirect the fd of self.cmd to a unix domain socket and os.setsid() somewhere after forking (twice maybe?).
# TODO: Make the environment variables used before launching self.cmd configurable

# Meta
__version__ = '0.9'
__license__ = "AGPLv3 or Proprietary (see LICENSE.txt)"
__version_info__ = (0, 9)
__author__ = 'Dan McDougall <daniel.mcdougall@liftoffsoftware.com>'

__doc__ = """\
About termio
============
This module provides a Multiplex class that can perform the following:

 * Fork a child process that opens a given terminal program.
 * Read and write data to and from the child process.
 * Log the output of the child process to a file and/or syslog.

The Multiplex class is meant to be used in conjunction with a running Tornado
IOLoop instance.  It can be instantiated from within your Tornado application
like so::

    multiplexer = termio.Multiplex(
        'nethack',
        tmpdir='/tmp',
        log_path='/var/log/myapp',
        user='bsmith@CORP',
        term_num=1,
        syslog=True
    )

Then *multiplexer* can create and launch a new controlling terminal (tty)
running the given command (e.g. 'nethack')::

    env = {
        'PATH': os.environ['PATH'],
        'MYVAR': 'foo'
    }
    fd = multiplexer.create(80, 24, env=env)
    # The fd is returned from create() in case you want more low-level control.

Input and output from the controlled program is asynchronous and gets handled
via IOLoop.  It will automatically write all output from the terminal program to
an instance of self.terminal_emulator (which defaults to Gate One's
terminal.Terminal).  So if you want to perform an action whenever the running
terminal application has output (like, say, sending a message to a client)
you'll need to attach a callback::

    def screen_update():
        'Called when new output is ready to send to the client'
        output = multiplexer.dumplines()
        socket_or_something.write(output)
    multiplexer.callbacks[multiplexer.CALLBACK_UPDATE] = screen_update

In this example, screen_update() will write() the output of
multiplexer.dumplines() to *socket_or_something* whenever the terminal program
has some sort of output.  You can also make calls directly to the terminal
emulator (if you're using a custom one)::

    def screen_update():
        output = multiplexer.term.my_custom_func()
        whatever.write(output)

Writing characters to the controlled terminal application is pretty
straightforward::

    multiplexer.proc_write('some text')

Typically you'd pass in keystrokes or commands from your application to the
underlying program this way and the screen/terminal emulator would get updated
automatically.  If using Gate One's Terminal() you can also attach callbacks
to perform further actions when more specific situations are encountered (e.g.
when the window title is set via that respective escape sequence)::

    def set_title():
        'Hypothetical title-setting function'
        print("Window title was just set to: %s" % multiplexer.term.title)
    multiplexer.term.callbacks[multiplexer.CALLBACK_TITLE] = set_title

Module Functions and Classes
============================
"""

# Stdlib imports
import signal, threading, fcntl, os, pty, re, sys, time, termios, struct
import io, codecs, gzip, syslog
from datetime import timedelta
from tornado import ioloop
from functools import partial
from itertools import izip
import logging
from subprocess import Popen
from multiprocessing.managers import BaseManager, SyncManager, RemoteError
# Fix missing termios.IUTF8
if 'IUTF8' not in termios.__dict__:
    termios.IUTF8 = 16384 # Hopefully this isn't platform independent

# Import our own stuff
from utils import noop
from utils import get_translation

_ = get_translation()

# Globals
SEPARATOR = u"\U000f0f0f" # The character used to separate frames in the log
# NOTE: That unicode character was carefully selected from only the finest
# of the PUA.  I hereby dub thee, "U+F0F0F0, The Separator."
CALLBACK_THREAD = None # Used by add_callback()

# Classes
class Multiplex:
    """
    The Multiplex class takes care of forking a child process and provides
    methods for reading/writing to it.  It also creates an instance of
    tornado.ioloop.IOLoop that listens for events on the spawned terminal
    application and updates self.proc[fd]['term'] with any changes.
    """
    CALLBACK_UPDATE = 1 # Screen update
    CALLBACK_EXIT = 2   # When the underlying program exits

    def __init__(self,
            cmd=None,
            terminal_emulator=None, # Defaults to Gate One's terminal.Terminal
            cps=5000,
            tmpdir="/tmp",
            log_path=None,
            user=None, # Only used by syslog output (to differentiate who's who)
            term_num=None, # Also only for syslog output for the same reason
            syslog=False,
            syslog_facility=syslog.LOG_DAEMON):
        self.lock = threading.RLock()
        # NOTE: Commented this out because Death apparently moves too swiftly!
        # Elect for automatic child reaping (may Death take them kindly!)
        #signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        self.cmd = cmd
        self.term_manager = None
        if not terminal_emulator:
            # Why do this?  So you could use/write your own specialty emulator.
            # Whatever you use it just has to accept 'rows' and 'cols' as
            # keyword arguments in __init__()
            from terminal import Terminal # Dynamic import to cut down on waste
            # Only in Python 2.7+ is functools.partial picklable so we can't use
            # multiprocessing in Python 2.6.
            #py_version = "%s.%s" % (sys.version_info[0], sys.version_info[1])
            #if sys.version_info[0] == 2 and sys.version_info[1] >= 7:
                ## Ahh, multiprocessing; how has thee gone unnoticed until now?
                #class TerminalManager(SyncManager):
                    #pass
                #TerminalManager.register('Term', Terminal)
                #self.term_manager = TerminalManager()
                #self.term_manager.start()
                #self.registry = self.term_manager.dict()
                #self.terminal_emulator = self.term_manager.Term
            #else:
                # Can't support as many simultaneous users but it works:
            self.terminal_emulator = Terminal
        else:
            self.terminal_emulator = terminal_emulator
        self.cps = cps # Characters per second where the rate limiter kicks in
        self.tmpdir = tmpdir # Where to store things like pid files
        self.log_path = log_path # Logs of the terminal output wind up here
        self.syslog = syslog # See "if self.syslog:" below
        self.syslog_facility = syslog_facility # Ditto
        self.io_loop = ioloop.IOLoop.instance() # Monitors child for activity
        self.io_loop.set_blocking_signal_threshold(4, self._blocked_io_handler)
        self.alive = True # Provides a quick way to see if we're still kickin'
        # These three variables are used by the rate limiting function:
        self.ratelimit = time.time()
        self.skip = False
        self.ratelimiter_engaged = False
        self.rows = 24
        self.cols = 80
        # Setup our callbacks
        self.callbacks = { # Defaults do nothing which saves some conditionals
            self.CALLBACK_UPDATE: {},
            self.CALLBACK_EXIT: {},
        }
        # Configure syslog logging
        self.user = user
        self.term_num = term_num
        self.syslog_buffer = ''
        if self.syslog: # Dynamic imports again because I'm freaky and frugal
            import syslog
            # Sets up syslog messages to show up like this:
            #   Sep 28 19:45:02 <hostname> gateone: <log message>
            syslog.openlog('gateone', 0, syslog_facility)

    def add_callback(self, event, callback, identifier=None):
        """
        Attaches the given *callback* to the given *event*.  If given,
        *identifier* can be used to reference this callback leter (e.g. when you
        want to remove it).  Otherwise an identifier will be generated
        automatically.  If the given *identifier* is already attached to a
        callback at the given event, that callback will be replaced with
        *callback*.

        *event* - The numeric ID of the event you're attaching *callback* to.
        *callback* - The function you're attaching to the *event*.
        *identifier* - A string or number to be used as a reference point should you wish to remove or update this callback later.

        Returns the identifier of the callback.  to Example:

            >>> m = Multiplex()
            >>> def somefunc(): pass
            >>> id = "myref"
            >>> ref = m.add_callback(m.CALLBACK_UPDATE, somefunc, id)

        NOTE: This allows us to attach callbacks when __init__() has ben
        overridden (e.g. multiprocessing.BaseManager).  It also allows the
        controlling program to have multiple callbacks for the same event.
        """
        if not identifier:
            identifier = callback.__hash__()
        self.callbacks[event][identifier] = callback
        return identifier

    def remove_callback(self, event, identifier):
        """
        Removes the callback referenced by *identifier* that is attached to the
        given *event*.  Example:

            >>> m.remove_callback(m.CALLBACK_BELL, "myref")

        """
        del self.callbacks[event][identifier]

    def _reenable_output(self):
        """
        Re-adds self.fd to the IOLoop so we can (hopefully) return to a running
        session.
        """
        self.ratelimiter_engaged = False
        # Empty the output queue.
        termios.tcflush(self.fd, termios.TCOFLUSH)
        #termios.tcflow(self.fd, termios.TCOON)
        with self.lock:
            with io.open(self.fd, 'rb', closefd=False) as reader:
                reader.read() # Go to the end
        #self.io_loop.add_handler(self.fd, self.proc_read, self.io_loop.READ)
        self.prev_output = [None for a in xrange(self.rows-1)]

    def _blocked_io_handler(self, signum, frame):
        """
        Handles the situation where a terminal is blocking IO with too much
        output.
        """
        logging.warning(
            "Noisy process kicked off rate limiter.  Sending Ctrl-c.")
        #os.kill(self.pid, signal.SIGINT)
        # Sending Ctrl-c via write() seems to work better:
        with io.open(self.fd, 'wb', closefd=False) as writer:
            writer.write("\x03 # Sent Ctrl-c\n") # Ctrl-c to the bad process
        # Turn off output to prevent the buffer from filling up
        #termios.tcflow(self.fd, termios.TCOOFF) # Doesn't seem to do anything
        # This doesn't seem to work either:
        #os.write(self.fd, "\x19") # Ctrl-S to the bad process
        # Empty the output queue.
        #termios.tcflush(self.fd, termios.TCIOFLUSH) # Ditto, nothing.
        # NOTE: Turns out that removing the IOLoop handler for this fd isn't
        # necessary...  It just slows down the process of killing the process.
        #self.io_loop.remove_handler(self.fd)
        self.ratelimiter_engaged = True
        for callback in self.callbacks[self.CALLBACK_UPDATE].values():
            self.io_loop.add_callback(callback)
        self.io_loop.add_timeout(timedelta(seconds=5), self._reenable_output)

    def create(self, rows=24, cols=80, env=None):
        """
        Creates a new virtual terminal (tty) and executes self.cmd within it.
        Also sets up our read/write callback and attaches them to Tornado's
        IOLoop.

        *cols*
            The number of columns to emulate on the virtual terminal (width)
        *rows*
            The number of rows to emulate (height).
        *env*
            A dictionary of environment variables to set when executing self.cmd.
        """
        pid, fd = pty.fork()
        if pid == 0: # We're inside the child process
    # Close all file descriptors other than stdin, stdout, and stderr (0, 1, 2)
            try:
                os.closerange(3, 256)
            except OSError:
                pass
            if not env:
                env = {}
            stdin = 0
            stdout = 1
            stderr = 2
            env["COLUMNS"] = str(cols)
            env["LINES"] = str(rows)
            env["TERM"] = "xterm" # TODO: This needs to be configurable on-the-fly
            #env["PATH"] = os.environ['PATH']
            #env["LANG"] = os.environ['LANG']
            # Setup stdout to be more Gate One friendly
            attrs = termios.tcgetattr(stdout)
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
            # Enable flow control and UTF-8 input (probably not needed)
            iflag |= (termios.IXON | termios.IXOFF | termios.IUTF8)
            # OPOST: Enable post-processing of chars (not sure if this matters)
            # INLCR: We're disabling this so we don't get \r\r\n anywhere
            oflag |= (termios.OPOST | termios.ONLCR | termios.INLCR)
            attrs = [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
            termios.tcsetattr(stdout, termios.TCSANOW, attrs)
            # Now do the same for stdin
            attrs = termios.tcgetattr(stdin)
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
            iflag |= (termios.IXON | termios.IXOFF | termios.IUTF8)
            oflag |= (termios.OPOST | termios.ONLCR | termios.INLCR)
            attrs = [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
            termios.tcsetattr(stdin, termios.TCSANOW, attrs)
            # Set non-blocking so we don't wait forever for a read()
            fcntl.fcntl(stdout, fcntl.F_SETFL, os.O_NONBLOCK)
            p = Popen(
                self.cmd,
                env=env,
                shell=True,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr)
            p.wait()
            # This exit() ensures IOLoop doesn't freak out about a missing fd
            sys.exit(0)
        else: # We're inside this Python script
            self.fd = fd
            self.pid = pid
            self.rows = rows
            self.cols = cols
            self.term = self.terminal_emulator(rows=rows, cols=cols)
            self.time = time.time()
            # Tell our IOLoop instance to start watching the child
            self.io_loop.add_handler(
                self.fd, self.proc_read, self.io_loop.READ)
            self.prev_output = [None for a in xrange(rows-1)]
            # (re)start the flow control on the fd
            termios.tcflow(fd, termios.TCOON)
            termios.tcflow(fd, termios.TCION)
            # Set non-blocking so we don't wait forever for a read()
            fcntl.fcntl(self.fd, fcntl.F_SETFL, os.O_NONBLOCK)
            # These two lines set the size of the terminal window:
            s = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, s)
            return fd

    def die(self):
        """
        Sets self.alive to False

        NOTE: This is actually important as it allows controlling processes to
        see if the multiplexer is still alive or not (so they don't have to
        enumerate the process table looking for a particular pid).
        """
        self.alive = False

    def resize(self, rows, cols):
        """
        Resizes the child process's terminal window to *rows* and *cols*
        """
        logging.debug("Resizing term %s to rows: %s, cols: %s" % (
            self.term_num, rows, cols))
        self.rows = rows
        self.cols = cols
        self.term.resize(rows, cols)
        s = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, s)

    def redraw(self):
        """
        Tells the running terminal program to redraw the screen by executing
        a window resize event (using its current dimensions) and writing a
        ctrl-l.
        """
        s = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, s)
        self.proc_write(u'\x0c') # ctrl-l
        # WINCH can mess things up quite a bit so I've disabled it.
        # Leaving this here in case it ever becomes configurable (reference)
        #os.kill(self.pid, signal.SIGWINCH)

    def proc_kill(self):
        """
        Kill the child process associated with the given file descriptor (fd).

        NOTE: If dtach is being used this only kills the dtach process.
        """
        try:
            self.io_loop.remove_handler(self.fd)
        except KeyError, IOError:
            # This can happen when the fd is removed by the underlying process
            # before the next cycle of the IOLoop.  Not really a problem.
            pass
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.wait()
        except OSError:
            # Lots of trivial reasons why we could get these
            pass
        if self.term_manager:
            try:
                self.term_manager.shutdown() # Don't want this running anymore
            except OSError:
                # Often happens when gateone.py terminates.  Nothing to see here.
                pass

    def term_write(self, chars):
        """
        Writes *chars* to self.term and also takes care of logging to
        *self.log_path* (if set) and/or syslog (if *self.syslog* is True).

        NOTE: This kind of logging doesn't capture user keystrokes.  This is
        intentional as we don't want passwords winding up in the logs.
        """
        #print("termio type of chars: %s" % type(chars))
        # Write to the log too (if configured)
        if self.log_path:
            now = int(round(time.time() * 1000))
            # NOTE: I'm using an obscure unicode symbol in order to avoid
            # conflicts.  We need to dpo our best to ensure that we can
            # differentiate between terminal output and our log format...
            # This should do the trick because it is highly unlikely that
            # someone would be displaying this obscure unicode symbol on an
            # actual terminal unless they were using Gate One to view a
            # Gate One log file in vim or something =)
            # \U000f0f0f == U+F0F0F (Private Use Symbol)
            #output = chars.decode("utf-8")
            output = unicode(chars.decode('utf-8', "ignore"))
            output = u"%s:%s\U000f0f0f" % (now, output)
            log = gzip.open(self.log_path, mode='a')
            log.write(output.encode("utf-8"))
            log.close()
        # NOTE: Gate One's log format is special in that it can be used for both
        # playing back recorded sessions *or* generating syslog-like output.
        if self.syslog:
            # Try and keep it as line-line as possible so we don't end up with
            # a log line per character.
            if '\n' in chars:
                for line in chars.splitlines():
                    if self.syslog_buffer:
                        line = self.syslog_buffer + line
                        self.syslog_buffer = ''
                    # Sylog really doesn't like any fancy encodings
                    line = line.encode('ascii', 'xmlcharrefreplace')
                    syslog.syslog("%s %s: %s" % (
                        self.user, self.term_num, line))
            else:
                self.syslog_buffer += chars
        try:
            self.term.write(chars)
        except RemoteError:
            logging.warning(
                "Got an error trying to write to the terminal emulator.  "
                "This probably means it died.")
        for callback in self.callbacks[self.CALLBACK_UPDATE].values():
            self.io_loop.add_callback(callback)

    def _buffer_to_term(self):
        """
        Reads the incoming stream from self.fd and writes it to the terminal
        emulator using self.term_write().
        """
        try:
            with self.lock:
                with io.open(self.fd, 'rb', closefd=False) as reader:
                    # This needs to be huge to handle big images
                    updated = bytearray(65536)
                    #updated = bytearray(1048568)
                    reader.readinto(updated)
                    updated = updated.rstrip('\x00') # Remove excess null chars
                self.term_write(updated)
        except IOError as e:
            # IOErrors can happen when self.fd is closed before we finish
            # writing to it.  Not a big deal.
            pass
        except OSError as e:
            logging.error("Got exception in proc_read: %s" % `e`)
        except Exception as e:
            import traceback
            logging.error("Got BIZARRO exception in proc_read (WTF?): %s" % `e`)
            traceback.print_exc(file=sys.stdout)
            self.die()
            self.proc_kill()

    def proc_read(self, fd, event):
        """
        Read in the output of the process associated with *fd* and write it to
        self.term.

        This method will also keep an eye on the output rate of the underlying
        terminal application.  If it goes to high (which would gobble up CPU) it
        will engage a rate limiter.  So if someone thinks it would be funny to
        run 'top' with a refresh rate of 0.01 they'll really only be getting
        updates every ~2 seconds (and it won't bog down the server =).

        NOTE: This method is not meant to be called directly...  The IOLoop
        should be the one calling it when it detects an io_loop.READ event.
        """
        if event == self.io_loop.READ:
            self._buffer_to_term()
        else: # Child died
            logging.debug("Apparently fd %s just died." % self.fd)
            self.die()
            self.proc_kill()
            for callback in self.callbacks[self.CALLBACK_EXIT].values():
                callback()

    def _buffer_write(self, chars):
        """
        Writes *chars* to self.fd (pretty straightforward).
        """
        with self.lock:
            try:
                with io.open(
                    self.fd,
                    'wt',
                    buffering=1024,
                    newline="",
                    encoding='UTF-8',
                    closefd=False
                ) as writer:
                    writer.write(chars)
            except (IOError, OSError):
                self.die()
                self.proc_kill()
            except Exception as e:
                logging.error("proc_write() exception: %s" % e)

    def proc_write(self, chars):
        """
        Adds _buffer_write(*chars*) to the IOLoop callback queue.
        """
        _buffer_write = partial(self._buffer_write, chars)
        self.io_loop.add_callback(_buffer_write)

    def dumplines(self, full=False):
        """
        Returns the terminal text lines (a list of lines, to be specific) and
        its scrollback buffer (which is also a list of lines) as a tuple,
        (scrollback, text).  If a line hasn't changed since the last dump then
        it will be replaced with an empty string (in the terminal text lines).

        If *full*, will return the entire screen (not just the diff).
        """
        try:
            output = []
            scrollback, html = ([], [])
            if self.term:
                try:
                    result = self.term.dump_html()
                    if result:
                        scrollback, html = result
                    else:
                        # This is just wacky and should not happen!
                        #logging.debug("Odd: result came back: %s" % result)
                        if self.term_manager:
                            # Re-create the terminal manager because it broke.
                            # I don't understand why this happens but it does.
                            self.term_manager.shutdown()
                            from terminal import Terminal
                            #import Queue
                            #queue = Queue.Queue()
                            class TerminalManager(BaseManager): pass
                            #TerminalManager.register(
                                #'get_queue', callable=lambda:queue)
                            TerminalManager.register('Term', Terminal)
                            self.term_manager = TerminalManager()
                            self.term_manager.start()
                            self.terminal_emulator = self.term_manager.Term
                            self.term = self.terminal_emulator(
                                rows=self.rows, cols=self.cols)
                        return([], [
                            "<b>The program output was too noisy so it has been"
                            " killed.  Press any key to continue...</b>"
                        ])
                except IOError as e:
                    logging.debug("IOError attempting self.term.dump_html()")
                    logging.debug("%s" % e)
                except RemoteError:
                    logging.debug("RemoteError in dumplines()")
                    # Terminal emulator shut down--probably because the
                    # associated process ended.  That's OK
                    return ([], [])
            if html:
                if not full:
                    for line1, line2 in izip(self.prev_output, html):
                        if line1 != line2:
                            output.append(line2)
                        else:
                            output.append('')
                        html = output
                    # Otherwise a full dump will take place
                self.prev_output = html
            return (scrollback, html)
        except ValueError as e:
            logging.error("ValueError in dumplines(): %s" % e)
            return ([], [])
        except (KeyError, IOError, TypeError) as e:
            import traceback
            logging.error("dumplines got exception: %s" % e)
            traceback.print_exc(file=sys.stdout)
            if self.ratelimiter_engaged:
                return([], [
                    "<b>Program output too noisy.  Sending Ctrl-c...</b>"])
            return ([], [])

#class CallbackThread(threading.Thread):
    #"""
    #Listens for events in a Queue() and calls any matching/registered callbacks.
    #"""
    ## NOTE: I know this CallbackThread/register_callback() stuff seems overly
    ## complex and it is...  But this was the simplest thing I could come up with
    ## to get callbacks working with multiprocessing.  If you have a better way
    ## please submit a patch!
    #def __init__(self, name):
        #threading.Thread.__init__(
            #self,
            #name=name
        #)
        #self.name = name
        #self.quitting = False
        #self.callbacks = {}

    #def register_callback(self, identifier, callback):
        #"""
        #Stores the given *callback* as self.callbacks[*identifier*]
        #"""
        #self.callbacks.update({identifier: callback})

    #def unregister_callbacks(self, callback_id):
        #"""
        #Removes all registered callbacks associated with *callback_id*
        #"""
        #for identifier in self.callbacks.keys():
            #if callback_id in identifier:
                #logging.debug("deleting self.callbacks[%s]" % identifier)
                #del self.callbacks[identifier]

    #def call_callback(self, identifier, args):
        #"""
        #Calls the callback function associated with *identifier* using the given
        #*args* like so::

            #self.callbacks[*identifier*](*args*)
        #"""
        #if identifier in self.callbacks:
            #if not args:
                #self.callbacks[identifier]()
            #else:
                #self.callbacks[identifier](*args)
        #else:
            #logging.error("Unmatched callback: %s" % identifier)

    #def quit(self):
        #logging.debug("CallbackThread got quit()")
        #try:
            #self.queue.put(('quit', None))
        #except (IOError, RemoteError) as e:
            ## The term emulator has already shut down.  Not a big deal
            ##logging.error("Got error in CallbackThread: %s" % e)
            #pass
        #self.quitting = True

    #def run(self):
        #while not self.quitting:
            #try:
                ## TODO: Figure out a way to re-attach the Queue to the callback
                ## thread in the event that the term_manager gets disconnected.
                #obj = self.queue.get()
                #identifier = obj[0]
                #args = obj[1]
                #if identifier != 'quit':
                    #self.call_callback(identifier, args)
            #except EOFError as e:
                #if not self.quitting:
                    #logging.error("EOFError in CallbackThread: %s" % e)
                    #logging.error("The EOFError likely means that the "
                                  #"term_manager died.")
            #except Exception as e:
                #if len("%s" % e): # Throws empty exceptions at quitting time.
                    #logging.error("Error in CallbackThread: %s" % e)
                #self.quitting = True
        #logging.info(_(
            #"{name} received quit()...  ".format(
                #name=self.name)
        #))