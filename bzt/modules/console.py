# coding=utf-8
"""
Copyright 2015 BlazeMeter Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from abc import abstractmethod
import re
import sys
import logging
import traceback
import math
import copy
import platform
import bzt

from logging import StreamHandler
from itertools import groupby
from datetime import datetime
from bzt.moves import StringIO

from urwid.decoration import Padding
from urwid.display_common import BaseScreen
from urwid import Text, Pile, WEIGHT, Filler, Columns, Widget, \
    CanvasCombine, LineBox, ListBox, RIGHT, CENTER, BOTTOM, CLIP, GIVEN
from urwid.font import Thin6x6Font
from urwid.graphics import BigText
from urwid.listbox import SimpleListWalker
from urwid.widget import Divider

from bzt.modules.provisioning import Local
from bzt.engine import Reporter, AggregatorListener
from bzt.modules.aggregator import DataPoint, KPISet

if platform.system() == 'Windows':
    from bzt.modules.screen import GUIScreen as Screen  # curses unavailable on windows
else:
    from urwid.curses_display import Screen


class ConsoleStatusReporter(Reporter, AggregatorListener):
    """
    Class to show process status on the console
    """
    # NOTE: maybe should use separate thread for screen re-painting
    def __init__(self):
        super(ConsoleStatusReporter, self).__init__()
        self.data_started = False
        self.logger_handler = None
        self.orig_stream = None
        self.screen_size = (140, 35)
        self.disabled = False
        self.temp_stream = StringIONotifying(self.log_updated)
        self.console = None
        self.screen = DummyScreen(self.screen_size[0], self.screen_size[1])

    def prepare(self):
        """
        Prepare console screen objects, logger, ask for widgets
        """
        super(ConsoleStatusReporter, self).prepare()
        self.disabled = self.settings.get("disable", False)
        if self.disabled:
            return

        if sys.stdout.isatty():
            self.screen = Screen()
            if platform.system() != 'Windows':
                self.__detect_console_logger()
        else:
            cols = self.settings.get('dummy-cols', self.screen_size[0])
            rows = self.settings.get('dummy-rows', self.screen_size[1])
            self.screen = DummyScreen(cols, rows)

        widgets = []
        if isinstance(self.engine.provisioning, Local):
            for executor in self.engine.provisioning.executors:
                if isinstance(executor, WidgetProvider):
                    widgets.append(executor.get_widget())
            for reporter in self.engine.reporters:
                if isinstance(reporter, WidgetProvider):
                    widgets.append(reporter.get_widget())

        self.console = TaurusConsole(widgets)
        self.screen.register_palette(self.console.palette)

    def check(self):
        """
        Repaint the screen
        """
        if self.disabled or not self.data_started:
            self.log.info("Test is running...")
            return False

        self.__start_screen()
        self.__update_screen()
        return False

    def __start_screen(self):
        """
        Start GUIScreen on windows or urwid.curses_display on *nix
        :return:
        """
        if self.data_started and not self.screen.started:
            if self.logger_handler:
                self.orig_stream = self.logger_handler.stream
                self.log.debug("Overriding logging stream")
                self.logger_handler.stream = self.temp_stream
            else:
                self.log.info("Did not mute console logging")

            self.screen.start()
            self.log.info("Waiting for finish...")

    def __update_screen(self):
        """
        update screen size, update log entries
        call screen.__repaint()
        :return:
        """
        if self.screen.started:
            self.console.tick()

            self.screen_size = self.screen.get_cols_rows()

            self.console.update_log(self.temp_stream)
            try:
                self.__repaint()
            except KeyboardInterrupt:
                raise
            except BaseException:
                self.log.error("Console screen failure: %s", traceback.format_exc())
                self.shutdown()

    def aggregated_second(self, data):
        """
        Consume aggregate data and feed it to console screen

        :type data: bzt.modules.aggregator.DataPoint
        :return:
        """
        if self.disabled:
            return

        try:
            self.console.add_data(data)
        except BaseException:
            self.log.warning("Failed to add datapoint to display")

        self.data_started = True

    def __dump_saved_log(self):
        """
        Dump data from background logging buffer to orig_stream
        """
        if self.logger_handler and self.orig_stream:
            # dump what we have in our background logging stream
            self.logger_handler.stream = self.orig_stream
            self.temp_stream.seek(0)
            self.orig_stream.write(self.temp_stream.getvalue())
            self.log.debug("Restored logging stream")
            self.logger_handler = None
        else:
            self.log.debug("No logger_handler or orig_stream was detected")

    def shutdown(self):
        """
        Stop showing the screen
        """
        super(ConsoleStatusReporter, self).shutdown()
        if self.disabled:
            return

        self.screen.stop()
        self.__dump_saved_log()

    def post_process(self):
        super(ConsoleStatusReporter, self).post_process()
        self.__dump_saved_log()

    def __detect_console_logger(self):
        logger = self.log
        while logger:
            for handler in logger.handlers[:]:
                if isinstance(handler, StreamHandler):
                    if handler.stream in (sys.stdout, sys.stderr):
                        # NOTE: assumed that we have only one stream handler
                        self.logger_handler = handler
                        break

            if logger.root == logger:
                break
            else:
                logger = logger.root

    def __repaint(self):
        if self.screen.started:
            canvas = self.console.render(self.screen_size, focus=False)
            self.screen.draw_screen(self.screen_size, canvas)

    def log_updated(self):
        """
        Notification for log changes, to repaint log widget
        """
        self.console.update_log(self.temp_stream)
        # we need to repaint, otherwise graceful shutdown messages not visible
        self.__repaint()


class ScrollingLog(ListBox):
    """
    Log widget that scrolls down automatically
    """
    ansi_escape = re.compile(r'\x1b[^m]*m')

    def __init__(self):
        body = SimpleListWalker([])
        super(ScrollingLog, self).__init__(body)
        self.last_size = (0, 0)

    def render(self, size, focus=False):
        """
        Render the widget

        :param size:
        :param focus:
        :return:
        """
        self.last_size = size
        while len(self.body) and BOTTOM not in self.ends_visible(size, focus):
            self.body.pop(0)
        return super(ScrollingLog, self).render(size, focus)

    def update(self, data):
        """
        Update log view with data

        :type data: str
        """
        lines = self.ansi_escape.sub('', data.strip()).split("\n")

        while len(self.body):
            self.body.pop(0)

        for line in lines[-self.last_size[1]:]:
            self.body.append(Text(('log', line)))


class TaurusConsole(Columns):
    """
    Root screen widget

    :type sidebar_widgets: list[widget.Widget]
    :type log_widget: ScrollingLog
    """
    palette = [
        ('sidebar', '', ''),
        ('log', '', ''),
        ('graph bg', '', ''),
        ('graph vu', 'light gray', 'light gray'),
        ('graph vc', 'brown', 'brown'),
        ('graph rps', 'dark green', 'dark green'),
        ('graph fail', 'dark red', 'dark red'),
        ('graph rt', 'dark blue', 'dark blue'),
        ('graph lt', 'dark cyan', 'dark cyan'),
        ('graph cn', 'dark magenta', 'dark magenta'),
        ('stat-hdr', 'light gray', 'dark blue'),
        ('stat-txt', '', ''),
        ('stat-2xx', 'light green', ''),
        ('stat-3xx', 'light cyan', ''),
        ('stat-4xx', 'brown', ''),
        ('stat-5xx', 'light red', ''),
        ('stat-nonhttp', 'light magenta', ''),
        ('pb-en', 'white', 'dark blue', ''),
        ('pb-dis', 'black', 'dark green', ''),
        ('pb-mid', 'brown', 'brown', ''),
        ('pf-3', 'yellow', ''),
        ('pf-4', 'light red', ''),
        ('pf-5', 'black', 'dark red'),
    ]

    def __init__(self, sidebar_widgets):
        self.log_widget = ScrollingLog()

        self.latest_stats = LatestStats()
        self.cumulative_stats = CumulativeStats()

        stats_pane = Pile([(WEIGHT, 0.33, self.latest_stats),
                           (WEIGHT, 0.67, self.cumulative_stats), ])

        self.graphs = ThreeGraphs()

        right_widgets = ListBox(SimpleListWalker([Pile([x, Divider()]) for x in sidebar_widgets]))

        self.logo = TaurusLogo()
        right_pane = Pile([(10, self.logo),
                           right_widgets,
                           (1, Filler(Divider('─'))),
                           (WEIGHT, 1, self.log_widget)])

        columns = [(WEIGHT, 0.25, self.graphs),
                   (WEIGHT, 0.50, stats_pane),
                   (WEIGHT, 0.25, right_pane)]
        super(TaurusConsole, self).__init__(columns)

    def add_data(self, data):
        """
        New datapoint notification

        :type data: bzt.modules.aggregator.DataPoint
        """
        overall = data[DataPoint.CURRENT].get('', KPISet())
        # self.log.debug("Got data for second: %s", to_json(data))

        active = int(math.floor(overall[KPISet.SAMPLE_COUNT] * overall[
            KPISet.AVG_RESP_TIME]))
        self.graphs.append(overall[KPISet.CONCURRENCY],
                           min(overall[KPISet.CONCURRENCY], active),
                           overall[KPISet.SAMPLE_COUNT],
                           overall[KPISet.FAILURES],
                           overall[KPISet.AVG_RESP_TIME],
                           overall[KPISet.AVG_CONN_TIME],
                           overall[KPISet.AVG_LATENCY], )

        self.latest_stats.add_data(data)
        self.cumulative_stats.add_data(data)

    def update_log(self, log_stream):
        """
        Update log with stream

        :type log_stream: bzt.modules.console.StringIONotifying
        """
        self.log_widget.update(log_stream.getvalue())

    def tick(self):
        """
        Update ticking widgets
        """
        self.logo.tick()


class DummyScreen(BaseScreen):
    """
    Null-object for Screen on non-tty output
    """

    def __init__(self, cols, rows):
        super(DummyScreen, self).__init__()
        self.size = (cols, rows)
        self.ansi_escape = re.compile(r'\x1b[^m]*m')

    def get_cols_rows(self):
        """
        Dummy cols and rows

        :return:
        """
        return self.size

    def draw_screen(self, size, canvas):
        """

        :param size:
        :type canvas: urwid.Canvas
        """
        data = ""
        for char in canvas.content():
            line = ""
            for part in char:
                if isinstance(part[2], str):
                    line += part[2]
                else:
                    line += part[2].decode()
            data += "%s│\n" % line
        data = self.ansi_escape.sub('', data)
        logging.info("Screen %sx%s chars:\n%s", size[0], size[1], data)


class StringIONotifying(StringIO, object):
    """
    StringIO extension that will call listener on every flush
    Note that by using it as logging stream there must be no logging
    calls inside listener, infinite recursion otherwise

    :param listener:
    """

    def __init__(self, listener):
        """

        :type self: StringIO
        """
        StringIO.__init__(self)
        self.listener = listener

    def flush(self):
        """

        :type self: StringIONotifying or StringIO
        """
        StringIO.flush(self)
        self.listener()


class ThreeGraphs(Pile):
    """
    Left pane of three graphs
    """

    def __init__(self, ):
        self.v_users = BoxedGraph(
            [' ', ("graph vu", '1'), " %s users, ",
             ("graph vc", '2'), " ~%s active "],
            ("graph bg", "graph vu", "graph vc"))
        self.rps = BoxedGraph([' ', ("graph rps", '1'), " %d hits, ",
                               ("graph fail", '2'), " %d fail "],
                              ("graph bg", "graph rps", "graph fail"))
        self.r_time = BoxedGraph([" ", ("graph rt", '1'), " %.3f avg time (",
                                  ("graph lt", '2'), " lat, ",
                                  ("graph cn", '3'), " conn) "],
                                 ("graph bg", "graph rt", "graph lt", "graph cn"))

        graphs = [self.v_users, self.rps, self.r_time]
        super(ThreeGraphs, self).__init__(graphs)

    def append(self, v_users, active, rps, fail, r_time, conn, lat):
        """
        Append data

        :type v_users: int
        :type active: int
        :type rps: int
        :type fail: int
        :type r_time: float
        :type conn: float
        :type lat: float
        """
        if v_users is None:
            v_users = 0
        if active is None:
            active = 0

        self.v_users.append((v_users, active))
        self.rps.append((rps, fail))
        self.r_time.append((r_time, lat, conn,))

        self._invalidate()


class StackedGraph(Widget):
    """
    Single stacked graph

    :type colors: tuple
    """

    def __init__(self, colors):
        super(StackedGraph, self).__init__()
        self.last_size = (0, 0)
        self.data = []
        self.max = 0.0
        self.colors = colors
        self.chars = ' .o@'

    def __get_matrix(self, cols, rows):
        aspect = max(self.max, 0.0000001) / float(rows)
        matrix = []
        for point in self.data[-cols:]:
            line = ''
            for idx, num in enumerate(point):
                chunk = str(idx + 1) * int(math.ceil(num / aspect))
                line = chunk + line[len(chunk):]
            line += '0' * (rows - len(line))
            matrix.append(line)

        while len(matrix) < cols:
            matrix.insert(0, '0' * rows)
        matrix = list(zip(*matrix))
        matrix.reverse()
        return matrix

    def render(self, size, focus=False):
        """
        Render the graph

        :param focus: ignored
        :type size: tuple
        :return:
        """
        self.last_size = size
        matrix = self.__get_matrix(size[0], size[1])

        rows = []
        for row in range(0, size[1]):
            line = []
            groups = ["".join(grp) for _num, grp in groupby(matrix[row])]
            for chunk in groups:
                color = self.colors[int(chunk[0])]
                char = self.chars[int(chunk[0])]
                line.append((color, len(chunk) * char))
            rows.append((Text(line).render((size[0],)), None, False))
        return CanvasCombine(rows)

    def append(self, value):
        """
        Add data to graph

        :type value: tuple[float] or float
        """
        if not isinstance(value, (list, tuple)):
            value = (value,)

        self.max = max(self.max, max(value))
        self.data.append(value)
        # self.set_data(self.data, max(self.max, 0.0000001))
        # self.set_title(self.caption % self.max)
        self._invalidate()


class BoxedGraph(LineBox):
    """
    Graph wrapped with LineBox

    :type title: list
    :type colors: tuple
    """

    def __init__(self, title, colors):
        self.graph = StackedGraph(colors)
        self.orig_title = title
        super(BoxedGraph, self).__init__(self.graph, title)

    def format_title(self, text):
        """
        Override title formatting

        :type text: list
        :return:
        """
        return text

    def append(self, data):
        """
        Append data, reflecting in title

        :type data: tuple
        """
        self.graph.append(data)
        nums = list(data)
        new_title = copy.copy(self.orig_title)
        for idx, part in enumerate(new_title):
            if '%' in part:
                new_title[idx] = part % nums.pop(0)
        self.set_title(new_title)


class LatestStats(LineBox):
    """
    Latest stats block
    """
    title = "Latest Interval Stats"

    def __init__(self):
        self.data = DataPoint(0)
        self.percentiles = PercentilesList(DataPoint.CURRENT)
        self.avg_times = AvgTimesList(DataPoint.CURRENT)
        self.rcodes = RCodesList(DataPoint.CURRENT)
        original_widget = Columns(
            [self.avg_times, self.percentiles, self.rcodes], dividechars=1)
        padded = Padding(original_widget, align=CENTER)
        super(LatestStats, self).__init__(padded,
                                          self.title)

    def add_data(self, data):
        """
        Append datapoint

        :type data: bzt.modules.aggregator.DataPoint
        """
        self.data = data
        if self.data[DataPoint.TIMESTAMP]:
            dat = datetime.fromtimestamp(self.data[DataPoint.TIMESTAMP])
            self.set_title(self.title + " at %s" % dat.strftime('%H:%M:%S'))

        self.percentiles.add_data(data)
        self.avg_times.add_data(data)
        self.rcodes.add_data(data)


class CumulativeStats(LineBox):
    """
    Cumulative stats block
    """

    def __init__(self):
        self.data = DataPoint(0)
        self.percentiles = PercentilesList(DataPoint.CUMULATIVE)
        self.avg_times = AvgTimesList(DataPoint.CUMULATIVE)
        self.rcodes = RCodesList(DataPoint.CUMULATIVE)
        self.labels_pile = LabelsPile(DataPoint.CUMULATIVE)
        original_widget = Pile([
            Columns([
                self.avg_times,
                self.percentiles,
                self.rcodes], dividechars=1),
            self.labels_pile
        ])
        padded = Padding(original_widget, align=CENTER)
        super(CumulativeStats, self).__init__(padded,
                                              "Cumulative Stats")

    def add_data(self, data):
        """
        Append datapoint

        :type data: bzt.modules.aggregator.DataPoint
        """
        self.data = data
        self.percentiles.add_data(data)
        self.avg_times.add_data(data)
        self.rcodes.add_data(data)
        self.labels_pile.add_data(data)


class PercentilesList(ListBox):
    """
    Percentile list

    :type key: str
    """

    def __init__(self, key):
        super(PercentilesList, self).__init__(SimpleListWalker([]))
        self.key = key

    def add_data(self, data):
        """
        Append data

        :type data: bzt.modules.aggregator.DataPoint
        """
        while len(self.body):
            self.body.pop(0)

        self.body.append(Text(("stat-hdr", " Percentiles: "), align=RIGHT))
        overall = data.get(self.key).get('', KPISet())
        for key in sorted(overall.get(KPISet.PERCENTILES).keys(), key=float):
            dat = (float(key), overall[KPISet.PERCENTILES][key])
            self.body.append(
                Text(("stat-txt", "%.1f%%: %.3f" % dat), align=RIGHT))


class AvgTimesList(ListBox):
    """
    Average times block

    :type key: str
    """

    def __init__(self, key):
        super(AvgTimesList, self).__init__(SimpleListWalker([]))
        self.key = key

    def add_data(self, data):
        """
        Append data

        :type data: bzt.modules.aggregator.DataPoint
        """
        while len(self.body):
            self.body.pop(0)

        self.body.append(Text(("stat-hdr", " Average Times: "), align=RIGHT))
        overall = data.get(self.key).get('', KPISet())
        recv = overall[KPISet.AVG_RESP_TIME]
        recv -= overall[KPISet.AVG_CONN_TIME]
        recv -= overall[KPISet.AVG_LATENCY]
        self.body.append(
            Text(("stat-txt", "Full: %.3f" % overall[KPISet.AVG_RESP_TIME]),
                 align=RIGHT))
        self.body.append(
            Text(("stat-txt", "Connect: %.3f" % overall[KPISet.AVG_CONN_TIME]),
                 align=RIGHT))
        self.body.append(
            Text(("stat-txt", "Latency: %.3f" % overall[KPISet.AVG_LATENCY]),
                 align=RIGHT))
        self.body.append(Text(("stat-txt", "~Receive: %.3f" % recv),
                              align=RIGHT))


class LabelsPile(Pile):
    """
    Label stats and error descriptions
    """

    def __init__(self, data):
        self.label_columns = LabelStatsTable(data)
        self.errors_description = DetailedErrorString(data)
        self.rows = [self.label_columns,
                     self.errors_description]
        super(LabelsPile, self).__init__(self.rows)

    def add_data(self, data):
        """
        add data to label columns and errors listbox
        """
        self.label_columns.add_data(data)
        self.errors_description.add_data(data)

    def render(self, size, focus=False):
        """
        Draws LabelsPile based on height of labels_column
        """
        labels_height = self.label_columns.get_height() + 1
        self.contents[0] = (self.contents[0][0], (GIVEN, labels_height))
        return super(LabelsPile, self).render(size, False)


class LabelStatsTable(Columns):
    """
    Sample labels block

    :type key: str
    """

    def __init__(self, key):
        self.labels = SampleLabelsNames()
        self.stats_table = StatsTable()
        self.columns = [self.labels,
                        self.stats_table]

        super(LabelStatsTable, self).__init__(self.columns, dividechars=1)
        self.key = key

    def add_data(self, data):
        """
        Append data

        :type data: bzt.modules.aggregator.DataPoint
        """
        self.labels.flush_data()
        self.stats_table.flush_data()

        overall = data.get(self.key)

        for label in overall.keys():
            if label != "":
                hits = overall.get(label).get(KPISet.SAMPLE_COUNT)
                failed = float(overall.get(label).get(KPISet.FAILURES)) / hits * 100 if hits else 0.0
                avg_rt = overall.get(label).get(KPISet.AVG_RESP_TIME)
                self.labels.add_data(label)
                self.stats_table.add_data(hits, failed, avg_rt)

    def render(self, size, focus=False):
        """
        render widget based on stat_table width
        if no space available, cut obtain some space from labels
        """
        max_width = size[0]
        stat_table_max_width = self.stats_table.get_width()
        label_names_width = self.labels.get_width()
        if stat_table_max_width + label_names_width <= max_width:
            self.contents[0] = (self.contents[0][0], (GIVEN, label_names_width, False))
        else:
            self.contents[0] = (self.contents[0][0], (GIVEN, max_width - stat_table_max_width, False))
        return super(LabelStatsTable, self).render(size, focus=False)

    def get_height(self):
        """
        Return widget's height
        """
        return self.labels.get_height()


class StatsTable(Columns):
    """
    Hits, Failures, AvgRT stats
    """

    def __init__(self):
        self.hits = SampleLabelsHits()
        self.failed = SampleLabelsFailed()
        self.avg_rt = SampleLabelsAvgRT()
        self.columns = [self.hits, (10, self.failed), (10, self.avg_rt)]
        super(StatsTable, self).__init__(self.columns, dividechars=1)

    def flush_data(self):
        """
        flush data from stats table columns
        """
        self.hits.flush_data()
        self.failed.flush_data()
        self.avg_rt.flush_data()

    def add_data(self, hits, failed, avg_rt):
        """
        add data to stats table columns
        """
        self.hits.add_data(hits)
        self.failed.add_data(failed)
        self.avg_rt.add_data(avg_rt)

    def get_width(self):
        """
        returns width of stats table widget
        """
        dividechars = 1
        table_size = self.hits.get_width() + self.columns[1][0] + self.columns[2][0] + dividechars * 3
        return table_size

    def render(self, size, focus=False):
        """
        set width for columns
        """
        hits_size = self.hits.get_width()
        self.contents[0] = (self.contents[0][0], (GIVEN, hits_size, False))
        return super(StatsTable, self).render(size, focus=False)


class StatsColumn(ListBox):
    """
    Abstract stats table column
    """

    def __init__(self, *args, **kargs):
        super(StatsColumn, self).__init__(*args, **kargs)

    def flush_data(self):
        """
        Erase data, draw header
        """
        while len(self.body):
            self.body.pop(0)
        self.body.append(self.header)

    def get_width(self):
        """
        get widget width
        """
        return max([len(x.text) for x in self.body])

    def get_height(self):
        """
        get widget height
        """
        return len(self.body)


class SampleLabelsNames(StatsColumn):
    """
    Stats table column with labels names
    """

    def __init__(self):
        super(SampleLabelsNames, self).__init__(SimpleListWalker([]))
        self.header = Text(("stat-hdr", " Labels "))
        self.body.append(self.header)

    def add_data(self, data):
        """
        add label name
        """
        data_widget = Text(("stat-txt", "%s" % data), wrap=CLIP)
        self.body.append(data_widget)


class SampleLabelsHits(StatsColumn):
    """
    Stats table column with hits
    """

    def __init__(self):
        super(SampleLabelsHits, self).__init__(SimpleListWalker([]))
        self.header = Text(("stat-hdr", " Hits "), align=RIGHT)
        self.body.append(self.header)

    def add_data(self, data):
        """
        add new hits value to column
        """
        data_widget = Text(("stat-txt", "%d" % data), align=RIGHT)
        self.body.append(data_widget)


class SampleLabelsFailed(StatsColumn):
    """
    Stats table column with fails
    """

    def __init__(self):
        super(SampleLabelsFailed, self).__init__(SimpleListWalker([]))
        self.header = Text(("stat-hdr", " Failures "), align=CENTER)
        self.body.append(self.header)

    def add_data(self, data):
        """
        add new failed value to column
        """
        data_widget = Text(("stat-txt", "%.2f%%" % data), align=RIGHT)
        self.body.append(data_widget)


class SampleLabelsAvgRT(StatsColumn):
    """
    Stats table column with average rt
    """

    def __init__(self):
        super(SampleLabelsAvgRT, self).__init__(SimpleListWalker([]))
        self.header = Text(("stat-hdr", " Avg Time "), align=RIGHT)
        self.body.append(self.header)

    def add_data(self, data):
        """
        add new avg rt value to column
        """
        data_widget = Text(("stat-txt", "%.3f" % data), align=RIGHT)
        self.body.append(data_widget)


class DetailedErrorString(ListBox):
    """

    :type key: str
    """

    def __init__(self, key):
        super(DetailedErrorString, self).__init__(SimpleListWalker([]))
        self.key = key

    def add_data(self, data):
        """
        Append data

        :type data: bzt.modules.aggregator.DataPoint
        """
        while len(self.body):
            self.body.pop(0)

        self.body.append(Text(("stat-hdr", " Errors: ")))
        overall = data.get(self.key)
        errors = overall.get('').get(KPISet.ERRORS)
        if errors:
            err_template = "{0} of: {1}"
            for error in sorted(errors, key=lambda _err: _err.get('cnt'), reverse=True):
                err_description = error.get('msg')
                err_count = error.get('cnt')

                self.body.append(
                    Text(("stat-txt", err_template.format(err_count, err_description)), wrap=CLIP))
        else:
            self.body.append(Text(("stat-txt", "No failures occured")))


class RCodesList(ListBox):
    """
    Response codes list

    :type key: str
    """

    def __init__(self, key):
        super(RCodesList, self).__init__(SimpleListWalker([]))
        self.key = key

    def add_data(self, data):
        """
        Append data point

        :type data: bzt.modules.aggregator.DataPoint
        """
        while len(self.body):
            self.body.pop(0)

        overall = data.get(self.key).get('', KPISet())

        self.body.append(Text(("stat-hdr", " Response Codes: "), align=RIGHT))

        for key in sorted(overall.get(KPISet.RESP_CODES).keys()):
            if overall[KPISet.SAMPLE_COUNT]:
                part = 100 * float(overall[KPISet.RESP_CODES][key]) / overall[
                    KPISet.SAMPLE_COUNT]
            else:
                part = 0

            dat = (
                key,
                part,
                overall[KPISet.RESP_CODES][key],
            )
            if key[0] == '2':
                style = 'stat-2xx'
            elif key[0] == '3':
                style = 'stat-3xx'
            elif key[0] == '4':
                style = 'stat-4xx'
            elif key[0] == '5':
                style = 'stat-5xx'
            else:
                style = "stat-nonhttp"
            self.body.append(
                Text((style, "%s:  %.2f%% (%s)" % dat), align=RIGHT))

        dat = (100, overall[KPISet.SAMPLE_COUNT])
        self.body.append(
            Text(('stat-txt', "All: %.2f%% (%s)" % dat), align=RIGHT))


class TaurusLogo(Pile):
    """
    Big taurus name
    """
    seq = r'/-\|'

    by_text = '%s v%s by BlazeMeter.com %s'

    def __init__(self):
        self.idx = 0
        b_txt = BigText("Taurus", Thin6x6Font())
        b_txt = Padding(b_txt, CENTER, width=CLIP)
        b_txt = Filler(b_txt)

        self.byb = Filler(Text('', align=CENTER))
        parts = [
            (5, b_txt),
            (1, self.byb),
        ]
        super(TaurusLogo, self).__init__(parts)

    def tick(self):
        """
        Update rotating sticks
        """
        txt = self.by_text % (self.seq[self.idx], bzt.VERSION, self.seq[self.idx])
        self.byb.body.set_text(txt)
        self.idx += 1
        if self.idx >= len(self.seq):
            self.idx = 0
        self._invalidate()


class WidgetProvider(object):
    """
    Mixin for classes that provide sidebar widgets
    """

    @abstractmethod
    def get_widget(self):
        """
        Returns widget instance to be added to sidebar

        :rtype: urwid.Widget
        """
        pass
