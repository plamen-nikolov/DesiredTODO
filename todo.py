# -*- coding: utf-8 -*-

## TODO: Implement TODO_IGNORE setting (http://mdeering.com/posts/004-get-your-textmate-todos-and-fixmes-under-control)
## TODO: Make the output clickable (à la find results)
## TODO: Occasional NoneType bug
## TODO: Make the sections foldable (define them as regions?)

""""""

import logging
import functools
import fnmatch
import re
import threading

import sublime
import sublime_plugin

from collections import namedtuple
from datetime import datetime
from itertools import groupby
from os import path, walk

DEBUG = True

Message = namedtuple('Message', 'type, msg')

## LOGGING SETUP
try:
    from logging import NullHandler
except ImportError:
    class NullHandler(logging.Handler):
        def handle(self, record):
            pass

        def emit(self, record):
            pass

        def createLock(self):
            self.lock = None

log = logging.getLogger('SublimeTODO')
log.handlers = []  # hack to prevent extraneous handlers on ST2 auto-reload
log.addHandler(NullHandler())
log.setLevel(logging.INFO)
if DEBUG:
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.DEBUG)


def do_when(conditional, callback, *args, **kwargs):
    if conditional():
        return callback(*args, **kwargs)
    sublime.set_timeout(functools.partial(
        do_when, conditional, callback, *args, **kwargs), 50)


class ThreadProgress(object):
    def __init__(self, thread, message, success_message, file_counter):
        self.thread = thread
        self.message = message
        self.success_message = success_message
        self.file_counter = file_counter
        self.addend = 1
        self.size = 8
        sublime.set_timeout(lambda: self.run(0), 100)

    def run(self, i):
        if not self.thread.is_alive():
            if hasattr(self.thread, 'result') and not self.thread.result:
                sublime.status_message('')
                return
            sublime.status_message(self.success_message)
            return

        before = i % self.size
        after = (self.size - 1) - before
        sublime.status_message('%s [%s=%s] (%s files scanned)' %
                              (self.message, ' ' * before, ' ' * after, self.file_counter))
        if not after:
            self.addend = -1
        if not before:
            self.addend = 1
        i += self.addend
        sublime.set_timeout(lambda: self.run(i), 100)


class TodoExtractor(object):
    def __init__(self, filepaths, dirpaths, **kwargs):
        self.__dict__.update(kwargs)
        self.filepaths = filepaths
        self.dirpaths = dirpaths
        self.log = logging.getLogger('SublimeTODO.extractor')

    def iter_files(self):
        """"""
        seen_paths_ = []
        files = self.filepaths
        dirs = self.dirpaths
        exclude_dirs = self.ignored_dirs

        for filepath in files:
            pth = path.realpath(path.abspath(filepath))
            if pth not in seen_paths_:
                seen_paths_.append(pth)
                yield pth

        for dirpath in dirs:
            dirpath = path.abspath(dirpath)
            for dirpath, dirnames, filenames in walk(dirpath):
                ## remove excluded dirs
                # TODO: These are not patterns. Consider making them glob
                # patterns
                for dir in exclude_dirs:
                    if dir in dirnames:
                        self.log.debug(u'SublimeTODO ignoring dir: {0}'.format(dir))
                        dirnames.remove(dir)

                for filepath in filenames:
                    pth = path.join(dirpath, filepath)
                    pth = path.realpath(path.abspath(pth))
                    if pth not in seen_paths_:
                        seen_paths_.append(pth)
                        yield pth

    def filter_files(self, files):
        """"""
        exclude_patterns = [re.compile(patt) for patt in self.ignored_files]

        for filepath in files:
            if any(patt.search(filepath) for patt in exclude_patterns):
                continue
            yield filepath

    def search_targets(self):
        """Yield filtered filepaths for message extraction"""
        return self.filter_files(self.iter_files())

    def extract(self):
        """"""
        message_patterns = '|'.join(self.patterns.values())
        case_sensitivity = 0 if not self.case_sensitive else re.IGNORECASE
        patt = re.compile(message_patterns, case_sensitivity)
        for filepath in self.search_targets():
            try:
                f = open(filepath, 'r', encoding='utf-8')
                self.log.debug(u'SublimeTODO scanning {0}'.format(filepath))
                for linenum, line in enumerate(f):
                    for mo in patt.finditer(line):
                        ## Remove the non-matched groups
                        matches = [Message(msg_type, msg) for msg_type,
                                   msg in mo.groupdict().items() if msg]
                        for match in matches:
                            yield {'filepath': filepath, 'linenum': linenum + 1, 'match': match}
            except (IOError, UnicodeDecodeError):
                ## Probably a broken symlink
                f = None
            finally:
                self.file_counter.increment()
                if f is not None:
                    f.close()


class RenderResultRunCommand(sublime_plugin.TextCommand):

    def run(self, edit, formatted_results, file_counter):
        # Figure out view
        active_window = sublime.active_window()
        view_name = 'TODO_RESULTS'
        existing_results = [v for v in active_window.views()
                            if v.name() == view_name and v.is_scratch()]
        if existing_results:
            result_view = existing_results[0]
        else:
            result_view = active_window.new_file()
            result_view.set_name(view_name)
            result_view.set_scratch(True)
            result_view.settings().set('todo_results', True)

        # Header
        hr = u'+ {0} +'.format('-' * 76)
        header = u'{hr}\n| TODOS @ {0:<68} |\n| {1:<76} |\n{hr}\n'.format(
            datetime.now().strftime('%A %d %B %Y %H:%M'),
            u'{0} files scanned'.format(file_counter),
            hr=hr)

        # result_view = self.view
        # edit = result_view.begin_edit()
        result_view.erase(edit, sublime.Region(0, result_view.size()))
        result_view.insert(edit, result_view.size(), header)
        # result_view.end_edit(edit)

        ## Region : match_dicts
        # 2 row list, where the first is region and the second is data
        regions_data = [x[:] for x in [[]] * 2]

        # ## Result sections
        for linetype, line, data in formatted_results:
            # edit = result_view.begin_edit()
            insert_point = result_view.size()
            result_view.insert(edit, insert_point, line)
            if linetype == 'result':
                rgn = sublime.Region(insert_point, result_view.size())
                regions_data[0].append(rgn)
                regions_data[1].append(data)
            result_view.insert(edit, result_view.size(), u'\n')
            # result_view.end_edit(edit)

        result_view.add_regions('results', regions_data[0], '')

        ## Store {Region : data} map in settings
        ## TODO: Abstract this out to a storage class Storage.get(region) ==> data dict
        ## Region() cannot be stored in settings, so convert to a primitive type
        # d_ = regions
        d_ = dict(('{0},{1}'.format(k.a, k.b), v)
            for k, v in zip(regions_data[0], regions_data[1]))
        result_view.settings().set('result_regions', d_)

        ## Set syntax and settings
        result_view.assign_syntax(
            'Packages/SublimeTODO/todo_results.hidden-tmLanguage')
        result_view.settings().set('line_padding_bottom', 2)
        result_view.settings().set('line_padding_top', 2)
        result_view.settings().set('word_wrap', False)
        result_view.settings().set('command_mode', True)
        active_window.focus_view(result_view)


class WorkerThread(threading.Thread):
    def __init__(self, extractor, callback, file_counter):
        self.extractor = extractor
        # self.renderer = renderer
        self.callback = callback
        self.file_counter = file_counter
        threading.Thread.__init__(self)

    def run(self):
        ## Extract in this thread
        todos = self.extractor.extract()
        formatted = list(self.format(todos))

        sublime.set_timeout(functools.partial(
            self.callback, formatted, self.file_counter), 10)

    def format(self, messages):
        """Yield lines for rendering into results view. Includes headers and
        blank lines.
        Lines are returned in the form (type, content, [data]) where type is either
        'header', 'whitespace' or 'result'
        """
        key_func = lambda m: m['match'].type
        messages = sorted(messages, key=key_func)

        for message_type, matches in groupby(messages, key=key_func):
            matches = list(matches)
            if matches:
                yield ('header', u'\n## {0} ({1})'.format(message_type.upper(), len(matches)), {})
                for idx, m in enumerate(matches, 1):
                    msg = m['match'].msg

                    filepath = path.basename(m['filepath'])
                    line = u"{idx}. {filepath}:{linenum} {msg}".format(
                        idx=idx, filepath=filepath, linenum=m['linenum'], msg=msg)
                    yield ('result', line, m)


class FileScanCounter(object):
    """Thread-safe counter used to update the status bar"""
    def __init__(self):
        self.ct = 0
        self.lock = threading.RLock()
        self.log = logging.getLogger('SublimeTODO')

    def __call__(self, filepath):
        self.log.debug(u'Scanning %s' % filepath)
        self.increment()

    def __str__(self):
        with self.lock:
            return '%d' % self.ct

    def increment(self):
        with self.lock:
            self.ct += 1

    def reset(self):
        with self.lock:
            self.ct = 0


class TodoCommand(sublime_plugin.TextCommand):
    """
    """
    def search_paths(self, window, paths, open_files_only=False, included_dirs_only=False):
        """Return (filepaths, dirpaths)"""

        if paths:
            return ([], paths)
        elif included_dirs_only:
            window = self.view.window()
            settings = self.view.settings().get('todo', {})
            included_dirs = settings.get('folder_include_patterns', [])
            global_settings = sublime.load_settings('Global.sublime-settings')
            included_dirs.extend(global_settings.get('folder_include_patterns', []))
            return ([], included_dirs)
        else:
            return (
                [view.file_name() for view in window.views() if view.file_name()],
                window.folders() if not open_files_only else []
            )

    def run(self, edit, paths=False, open_files_only=False, included_dirs_only=False):
        view = self.view
        window = view.window()
        settings = {}

        ## Load settings files
        plugin_settings = sublime.load_settings('SublimeTODO.sublime-settings')
        global_settings = sublime.load_settings('Global.sublime-settings')

        ## Prepare the settings required for renderer and extractor
        ## TODO: Consider moving this to an object that is maintained with on_change

        ## Merge core_patterns and patterns
        settings['patterns'] = plugin_settings.get('core_patterns', {})
        settings['patterns'].update(plugin_settings.get('patterns'))

        settings['case_sensitive'] = 0 if plugin_settings.get('case_sensitive', False) else re.IGNORECASE
        settings['ignored_dirs'] = plugin_settings.get('folder_exclude_patterns', [])
        settings['title'] = plugin_settings.get('result_title')

        ## File exclude patterns merged with binary file patterns
        exclude_file_patterns = plugin_settings.get('file_exclude_patterns', [])
        exclude_file_patterns.extend(global_settings.get('file_exclude_patterns', []))
        exclude_file_patterns.extend(global_settings.get('binary_file_patterns', []))
        exclude_file_patterns = [fnmatch.translate(patt) for patt in exclude_file_patterns]
        settings['ignored_files'] = exclude_file_patterns

        filepaths, dirpaths = self.search_paths(window, paths, open_files_only=open_files_only, included_dirs_only=included_dirs_only)

        ignored_dirs = settings.get('folder_exclude_patterns', [])
        ## Get exclude patterns from global settings
        ## Is there really no better way to access global settings?
        global_settings = sublime.load_settings('Global.sublime-settings')
        ignored_dirs.extend(global_settings.get('folder_exclude_patterns', []))

        file_counter = FileScanCounter()
        settings['file_counter'] = file_counter

        extractor = TodoExtractor(filepaths, dirpaths, **settings)

        # NOTE: TodoRenderer class was disassembled and codes are moved
        # to WorkerThread and RenderResultRunCommand
        # renderer = TodoRenderer(settings, window, file_counter)

        worker_thread = WorkerThread(extractor, self.render_formatted, file_counter)
        worker_thread.start()
        ThreadProgress(worker_thread, 'Finding TODOs', '', file_counter)

    def render_formatted(self, rendered, counter):
        self.view.run_command("render_result_run",
                              {"formatted_results": rendered, "file_counter": str(counter)})


class NavigateResults(sublime_plugin.TextCommand):
    DIRECTION = {'forward': 1, 'backward': -1}
    STARTING_POINT = {'forward': -1, 'backward': 0}

    def __init__(self, view):
        super(NavigateResults, self).__init__(view)
        self.log = logging.getLogger('SublimeTODO.NavigateResults')

    def run(self, edit, direction):
        view = self.view
        settings = view.settings()
        results = self.view.get_regions('results')
        self.log.debug(settings.get('selected_result'))
        if not results:
            sublime.status_message('No results to navigate')
            return

        # NOTE: numbers stored in settings are coerced to floats or longs
        selection = int(
            settings.get('selected_result', self.STARTING_POINT[direction]))
        selection = selection + self.DIRECTION[direction]
        try:
            target = results[selection]
        except IndexError:
            target = results[0]
            selection = 0

        self.log.debug(target)
        settings.set('selected_result', selection)
        ## Create a new region for highlighting
        target = target.cover(target)
        view.add_regions('selection', [target], 'selected', 'dot')
        view.show(target)


class ClearSelection(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.erase_regions('selection')
        self.view.settings().erase('selected_result')


class GotoComment(sublime_plugin.TextCommand):
    def __init__(self, *args):
        self.log = logging.getLogger('SublimeTODO.nav')
        super(GotoComment, self).__init__(*args)

    def run(self, edit):
        view = self.view
        ## Get the idx of selected result region
        selection = int(view.settings().get('selected_result', -1))
        ## Get the region
        selected_region = self.view.get_regions('results')[selection]
        ## Convert region to key used in result_regions (this is tedious, but
        ##    there is no other way to store regions with associated data)
        data = self.view.settings().get('result_regions')['{0},{1}'.format(
            selected_region.a, selected_region.b)]
        self.log.debug(u'Goto comment at {filepath}:{linenum}'.format(**data))
        new_view = self.view.window().open_file(data['filepath'])
        do_when(lambda: not new_view.is_loading(), lambda:
                new_view.run_command("goto_line", {"line": data['linenum']}))


class MouseGotoComment(sublime_plugin.TextCommand):
    def __init__(self, *args):
        self.log = logging.getLogger('SublimeTODO.nav')
        super(MouseGotoComment, self).__init__(*args)

    def highlight(self, region):
        target = region.cover(region)
        self.view.add_regions('selection', [target], 'selected', 'dot')
        self.view.show(target)

    def get_result_region(self, pos):
        line = self.view.line(pos)
        return line

    def run(self, edit):
        if not self.view.settings().get('result_regions'):
            return
        ## get selected line
        pos = self.view.sel()[0].end()
        result = self.get_result_region(pos)
        self.highlight(result)
        data = self.view.settings().get('result_regions')[
            '{0},{1}'.format(result.a, result.b)]
        self.log.debug(u'Goto comment at {filepath}:{linenum}'.format(**data))
        new_view = self.view.window().open_file(data['filepath'])
        do_when(lambda: not new_view.is_loading(), lambda:
                new_view.run_command("goto_line", {"line": data['linenum']}))
