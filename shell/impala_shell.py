#!/usr/bin/env python
# Copyright 2012 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# Impala's shell
import cmd
import time
import sys
import os
import signal
import threading
from optparse import OptionParser

from beeswaxd import BeeswaxService
from beeswaxd.BeeswaxService import QueryState
from ImpalaService import ImpalaService
from ImpalaService.ImpalaService import TImpalaQueryOptions
from ImpalaService.constants import DEFAULT_QUERY_OPTIONS
from Status.ttypes import TStatus, TStatusCode
from thrift.transport.TSocket import TSocket
from thrift.transport.TTransport import TBufferedTransport, TTransportException
from thrift.protocol import TBinaryProtocol
from thrift.Thrift import TApplicationException

VERSION_FORMAT = "Impala v%(version)s (%(git_hash)s) built on %(build_date)s"
COMMENT_TOKEN = '--'
VERSION_STRING = "build version not available"
HISTORY_LENGTH = 100

# Tarball / packaging build makes impala_build_version available
try:
  from impala_build_version import get_git_hash, get_build_date, get_version
  VERSION_STRING = VERSION_FORMAT % {'version': get_version(),
                                     'git_hash': get_git_hash()[:7],
                                     'build_date': get_build_date()}
except Exception:
  pass

class RpcStatus:
  """Convenience enum to describe Rpc return statuses"""
  OK = 0
  ERROR = 1

# Simple Impala shell. Can issue queries (with configurable options)
# Basic usage: type connect <host:port> to connect to an impalad
# Then issue queries or other commands. Tab-completion should show the set of
# available commands.
# Methods that implement shell commands return a boolean tuple (stop, status)
# stop is a flag the command loop uses to continue/discontinue the prompt.
# Status tells the caller that the command completed successfully.
# TODO: (amongst others)
#   - Column headers / metadata support
#   - Report profiles
#   - A lot of rpcs return a verbose TStatus from thrift/Status.thrift
#     This will be useful for better error handling. The next iteration
#     of the shell should handle this return paramter.
class ImpalaShell(cmd.Cmd):
  DISCONNECTED_PROMPT = "[Not connected] > "

  def __init__(self, options):
    cmd.Cmd.__init__(self)
    self.is_alive = True
    self.use_kerberos = options.use_kerberos
    self.verbose = options.verbose
    self.kerberos_service_name = options.kerberos_service_name
    self.impalad = None
    self.prompt = ImpalaShell.DISCONNECTED_PROMPT
    self.connected = False
    self.imp_service = None
    self.transport = None
    self.fetch_batch_size = 1024
    self.query_options = {}
    self.__make_default_options()
    self.query_state = QueryState._NAMES_TO_VALUES
    self.refresh_after_connect = options.refresh_after_connect
    self.default_db = options.default_db
    self.history_file = os.path.expanduser("~/.impalahistory")
    try:
      self.readline = __import__('readline')
      self.readline.set_history_length(HISTORY_LENGTH)
    except ImportError:
      self.readline = None
    if options.impalad != None:
      self.do_connect(options.impalad)

    # We handle Ctrl-C ourselves, using an Event object to signal cancellation
    # requests between the handler and the main shell thread
    self.is_interrupted = threading.Event()
    signal.signal(signal.SIGINT, self.__signal_handler)

  def __get_option_name(self, option):
    return TImpalaQueryOptions._VALUES_TO_NAMES[option]

  def __make_default_options(self):
    self.query_options = {}
    for option, default in DEFAULT_QUERY_OPTIONS.iteritems():
      self.query_options[self.__get_option_name(option)] = default

  def __print_options(self):
    print '\n'.join(["\t%s: %s" % (k,v) for (k,v) in self.query_options.iteritems()])

  def __options_to_string_list(self):
    return ["%s=%s" % (k,v) for (k,v) in self.query_options.iteritems()]

  def do_shell(self, args):
    """Run a command on the shell
    Usage: shell <cmd>
           ! <cmd>

    """
    try:
      os.system(args)
    except Exception, e:
      print 'Error running command : %s' % e
    return True

  def sanitise_input(self, args):
    """Convert the command to lower case, so it's recognized"""
    # A command terminated by a semi-colon is legal. Check for the trailing
    # semi-colons and strip them from the end of the command.
    args = args.strip()
    tokens = args.split(' ')
    # The first token should be the command
    # If it's EOF, call do_quit()
    if tokens[0] == 'EOF':
      return 'quit'
    else:
      tokens[0] = tokens[0].lower()
    return ' '.join(tokens).rstrip(';')

  def __signal_handler(self, signal, frame):
    self.is_interrupted.set()

  def precmd(self, args):
    self.is_interrupted.clear()
    return self.sanitise_input(args)

  def postcmd(self, status, args):
    """Hack to make non interactive mode work"""
    self.is_interrupted.clear()
    # cmd expects return of False to keep going, and True to quit.
    # Shell commands return True on success, False on error, and None to quit, so
    # translate between them.
    # TODO : Remove in the future once shell and Impala query processing can be separated.
    if status == None:
      return True
    else:
      return False

  def do_set(self, args):
    """Set or display query options.

    Display query options:
    Usage: SET
    Set query options:
    Usage: SET <option>=<value>

    """
    # TODO: Expand set to allow for setting more than just query options.
    if len(args) == 0:
      self.__print_if_verbose("Impala query options:")
      self.__print_options()
      return True

    tokens = args.split("=")
    if len(tokens) != 2:
      print "Error: SET <option>=<value>"
      return False
    option_upper = tokens[0].upper()
    if option_upper not in ImpalaService.TImpalaQueryOptions._NAMES_TO_VALUES.keys():
      print "Unknown query option: %s" % (tokens[0],)
      available_options = \
          '\n\t'.join(ImpalaService.TImpalaQueryOptions._NAMES_TO_VALUES.keys())
      print "Available query options are: \n\t%s" % available_options
      return False
    self.query_options[option_upper] = tokens[1]
    self.__print_if_verbose('%s set to %s' % (option_upper, tokens[1]))
    return True

  def do_quit(self, args):
    """Quit the Impala shell"""
    self.__print_if_verbose("Goodbye")
    self.is_alive = False
    # None is crutch to tell shell loop to quit
    return None

  def do_connect(self, args):
    """Connect to an Impalad instance:
    Usage: connect <hostname:port>

    """
    tokens = args.split(" ")
    if len(tokens) != 1:
      print "CONNECT takes exactly one argument: <hostname:port> of impalad to connect to"
      return False
    try:
      connection_params = tokens[0].split(':')
      if len(connection_params) > 1:
        host, port = connection_params
      else:
        host, port = connection_params[0], 21000
      self.impalad = (host, port)
    except ValueError:
      print "Connect string must be of form <hostname:port>"
      return False

    if self.__connect():
      self.__print_if_verbose('Connected to %s:%s' % self.impalad)
      self.prompt = "[%s:%s] > " % self.impalad
      if self.refresh_after_connect:
        self.cmdqueue.append('refresh')
      if self.default_db:
        self.cmdqueue.append('use %s' % self.default_db)
    return True

  def __connect(self):
    if self.transport is not None:
      self.transport.close()
      self.transport = None

    self.connected = False
    try:
      self.transport = self.__get_transport()
      self.transport.open()
      protocol = TBinaryProtocol.TBinaryProtocol(self.transport)
      self.imp_service = ImpalaService.Client(protocol)
      try:
        self.imp_service.PingImpalaService()
        self.connected = True
      except Exception, e:
        print ("Error: Unable to communicate with impalad service. This service may not "
               "be an impalad instance. Check host:port and try again.")
        self.transport.close()
        raise
    except Exception, e:
      print "Error connecting: %s, %s" % (type(e),e)

    return self.connected

  def __get_transport(self):
    """Create a Transport.

       A non-kerberized impalad just needs a simple buffered transport. For
       the kerberized version, a sasl transport is created.
    """
    sock = TSocket(self.impalad[0], int(self.impalad[1]))
    if not self.use_kerberos:
      return TBufferedTransport(sock)
    # Initializes a sasl client
    def sasl_factory():
      sasl_client = sasl.Client()
      sasl_client.setAttr("host", self.impalad[0])
      sasl_client.setAttr("service", self.kerberos_service_name)
      sasl_client.init()
      return sasl_client
    # GSSASPI is the underlying mechanism used by kerberos to authenticate.
    return TSaslClientTransport(sasl_factory, "GSSAPI", sock)

  def __get_sleep_interval(self, start_time):
    """Returns a step function of time to sleep in seconds before polling
    again. Maximum sleep is 1s, minimum is 0.1s"""
    elapsed = time.time() - start_time
    if elapsed < 10.0:
      return 0.1
    elif elapsed < 60.0:
      return 0.5

    return 1.0

  def __query_with_results(self, query):
    self.__print_if_verbose("Query: %s" % (query.query,))
    start, end = time.time(), 0
    (handle, status) = self.__do_rpc(lambda: self.imp_service.query(query))

    if self.is_interrupted.isSet():
      if status == RpcStatus.OK:
        self.__close_query_handle(handle)
      return False
    if status != RpcStatus.OK:
      return False

    loop_start = time.time()
    while True:
      query_state = self.__get_query_state(handle)
      if query_state == self.query_state["FINISHED"]:
        break
      elif query_state == self.query_state["EXCEPTION"]:
        print 'Query aborted, unable to fetch data'
        if self.connected:
          return self.__close_query_handle(handle)
        else:
          return False
      elif self.is_interrupted.isSet():
        return self.__cancel_query(handle)
      time.sleep(self.__get_sleep_interval(loop_start))

    # Results are ready, fetch them till they're done.
    self.__print_if_verbose('Query finished, fetching results ...')
    result_rows = []
    num_rows_fetched = 0
    while True:
      # Fetch rows in batches of at most fetch_batch_size
      (results, status) = self.__do_rpc(lambda: self.imp_service.fetch(
                                                  handle, False, self.fetch_batch_size))

      if self.is_interrupted.isSet() or status != RpcStatus.OK:
        # Worth trying to cleanup the query even if fetch failed
        if self.connected:
          self.__close_query_handle(handle)
        return False
      num_rows_fetched += len(results.data)
      result_rows.extend(results.data)
      if len(result_rows) >= self.fetch_batch_size or not results.has_more:
        print '\n'.join(result_rows)
        result_rows = []
        if not results.has_more:
          break
    end = time.time()

    self.__print_if_verbose(
      "Returned %d row(s) in %2.2fs" % (num_rows_fetched, end - start))
    return self.__close_query_handle(handle)

  def __close_query_handle(self, handle):
    """Close the query handle"""
    self.__do_rpc(lambda: self.imp_service.close(handle))
    return True

  def __print_if_verbose(self, message):
    if self.verbose:
      print message

  def do_select(self, args):
    """Executes a SELECT... query, fetching all rows"""
    query = BeeswaxService.Query()
    query.query = "select %s" % (args,)
    query.configuration = self.__options_to_string_list()
    return self.__query_with_results(query)

  def do_use(self, args):
    """Executes a USE... query"""
    query = BeeswaxService.Query()
    query.query = "use %s" % (args,)
    query.configuration = self.__options_to_string_list()
    return self.__query_with_results(query)

  def do_show(self, args):
    """Executes a SHOW... query, fetching all rows"""
    query = BeeswaxService.Query()
    query.query = "show %s" % (args,)
    query.configuration = self.__options_to_string_list()
    return self.__query_with_results(query)

  def do_describe(self, args):
    """Executes a DESCRIBE... query, fetching all rows"""
    query = BeeswaxService.Query()
    query.query = "describe %s" % (args,)
    query.configuration = self.__options_to_string_list()
    return self.__query_with_results(query)

  def do_insert(self, args):
    """Executes an INSERT query"""
    query = BeeswaxService.Query()
    query.query = "insert %s" % (args,)
    query.configuration = self.__options_to_string_list()
    print "Query: %s" % (query.query,)
    start, end = time.time(), 0
    (handle, status) = self.__do_rpc(lambda: self.imp_service.query(query))

    if status != RpcStatus.OK:
      return False

    while True:
      query_state = self.__get_query_state(handle)
      if query_state == self.query_state["FINISHED"]:
        break
      elif query_state == self.query_state["EXCEPTION"]:
        print 'Remote error'
        if self.connected:
          # Retrieve error message (if any) from log.
          log, status = self._ImpalaShell__do_rpc(
            lambda: self.imp_service.get_log(handle.log_context))
          print log,
          # It's ok to close an INSERT that's failed rather than do the full
          # CloseInsert. The latter builds an InsertResult which is meaningless
          # here.
          return self.__close_query_handle(handle)
        else:
          return False
      elif self.is_interrupted.isSet():
        return self.__cancel_query(handle)
      time.sleep(0.05)

    (insert_result, status) = self.__do_rpc(lambda: self.imp_service.CloseInsert(handle))
    end = time.time()
    if status != RpcStatus.OK or self.is_interrupted.isSet():
      return False

    num_rows = sum([int(k) for k in insert_result.rows_appended.values()])
    self.__print_if_verbose("Inserted %d rows in %2.2fs" % (num_rows, end - start))
    return True

  def __cancel_query(self, handle):
    """Cancel a query on a keyboard interrupt from the shell."""
    print 'Cancelling query ...'
    # Cancel sets query_state to EXCEPTION before calling cancel() in the
    # co-ordinator, so we don't need to wait.
    (_, status) = self.__do_rpc(lambda: self.imp_service.Cancel(handle))
    if status != RpcStatus.OK:
      return False

    return True

  def __get_query_state(self, handle):
    state, status = self.__do_rpc(lambda : self.imp_service.get_state(handle))
    if status != RpcStatus.OK:
      return self.query_state["EXCEPTION"]
    return state

  def __do_rpc(self, rpc):
    """Executes the RPC lambda provided with some error checking. Returns
       (rpc_result, RpcStatus.OK) if request was successful,
       (None, RpcStatus.ERROR) otherwise.

       If an exception occurs that cannot be recovered from, the connection will
       be closed and self.connected will be set to False.

    """
    if not self.connected:
      print "Not connected (use CONNECT to establish a connection)"
      return (None, RpcStatus.ERROR)
    try:
      ret = rpc()
      status = RpcStatus.OK
      # TODO: In the future more advanced error detection/handling can be done based on
      # the TStatus return value. For now, just print any error(s) that were encountered
      # and validate the result of the operation was a succes.
      if ret is not None and isinstance(ret, TStatus):
        if ret.status_code != TStatusCode.OK:
          if ret.error_msgs:
            print 'RPC Error: %s' % '\n'.join(ret.error_msgs)
          status = RpcStatus.ERROR
      return (ret, status)
    except BeeswaxService.QueryNotFoundException, q:
      print 'Error: Stale query handle'
    # beeswaxException prints out the entire object, printing
    # just the message a far more readable/helpful.
    except BeeswaxService.BeeswaxException, b:
      print "ERROR: %s" % (b.message,)
    except TTransportException, e:
      print "Error communicating with impalad: %s" % (e,)
      self.connected = False
      self.prompt = ImpalaShell.DISCONNECTED_PROMPT
    except TApplicationException, t:
      print "Application Exception : %s" % (t,)
    except Exception, u:
      print 'Unknown Exception : %s' % (u,)
      self.connected = False
      self.prompt = ImpalaShell.DISCONNECTED_PROMPT
    return (None, RpcStatus.ERROR)

  def do_explain(self, args):
    """Explain the query execution plan"""
    query = BeeswaxService.Query()
    # Args is all text except for 'explain', so no need to strip it out
    query.query = args
    query.configuration = self.__options_to_string_list()
    print "Explain query: %s" % (query.query,)
    (explanation, status) = self.__do_rpc(lambda: self.imp_service.explain(query))
    if status != RpcStatus.OK:
      return False

    print explanation.textual
    return True

  def do_refresh(self, args):
    """Reload the Impalad catalog"""
    (_, status) = self.__do_rpc(lambda: self.imp_service.ResetCatalog())
    if status != RpcStatus.OK:
      return False

    print "Successfully refreshed catalog"
    return True

  def do_history(self, args):
    """Display command history"""
    # Deal with readline peculiarity. When history does not exists,
    # readline returns 1 as the history length and stores 'None' at index 0.
    if self.readline and self.readline.get_current_history_length() > 0:
      for index in xrange(1, self.readline.get_current_history_length() + 1):
        print '[%d]: %s' % (index, self.readline.get_history_item(index))
    else:
      print 'readline module not found, history is not supported.'
    return True

  def preloop(self):
    """Load the history file if it exists"""
    if self.readline:
      try:
        self.readline.read_history_file(self.history_file)
      except IOError, i:
        print 'Unable to load history: %s' % i

  def postloop(self):
    """Save session commands in history."""
    if self.readline:
      try:
        self.readline.write_history_file(self.history_file)
      except IOError, i:
        print 'Unable to save history: %s' % i

  def default(self, args):
    print "Unrecognized command"
    return True

  def emptyline(self):
    """If an empty line is entered, do nothing"""
    return True

  def do_version(self, args):
    """Prints the Impala build version"""
    print "Build version: %s" % VERSION_STRING
    return True

WELCOME_STRING = """Welcome to the Impala shell. Press TAB twice to see a list of \
available commands.

Copyright (c) 2012 Cloudera, Inc. All rights reserved.

(Build version: %s)""" % VERSION_STRING

def parse_query_text(query_text):
  """Parse query file text and filter out the queries.

  This method filters comments. Comments can be of 3 types:
  (a) select foo --comment
      from bar;
  (b) select foo
      from bar --comment;
  (c) --comment
  The semi-colon takes precedence over everything else. As such,
  it's not permitted within a comment, and cannot be escaped.
  """
  # queries are split by a semi-colon.
  raw_queries = query_text.split(';')
  queries = []
  for raw_query in raw_queries:
    query = []
    for line in raw_query.split('\n'):
      line = line.split(COMMENT_TOKEN)[0].strip()
      if len(line) > 0:
        # anything before the comment is legal.
        query.append(line)
    queries.append('\n'.join(query))
  # The last query need not be demilited by a semi-colon.
  # If it is, get rid of the last element.
  if len(queries[-1]) == 0:
    queries = queries[:-1]
  return queries

def execute_queries_non_interactive_mode(options):
  """Run queries in non-interactive mode."""
  queries = []
  if options.query_file:
    try:
      query_file_handle = open(options.query_file, 'r')
      queries = parse_query_text(query_file_handle.read())
      query_file_handle.close()
    except Exception, e:
      print 'Error: %s' % e
      sys.exit(1)
  elif options.query:
    queries = [options.query]
  shell = ImpalaShell(options)
  # The impalad was specified on the command line and the connection failed.
  # Return with an error, no need to process the query.
  if options.impalad and shell.connected == False:
    sys.exit(1)
  queries = shell.cmdqueue + queries
  # Deal with case.
  queries = map(shell.sanitise_input, queries)
  for query in queries:
    if not shell.onecmd(query):
      print 'Could not execute command: %s' % query
      if not options.ignore_query_failure:
        sys.exit(1)

if __name__ == "__main__":
  parser = OptionParser()
  parser.add_option("-i", "--impalad", dest="impalad", default=None,
                    help="<host:port> of impalad to connect to")
  parser.add_option("-q", "--query", dest="query", default=None,
                    help="Execute a query without the shell")
  parser.add_option("-f", "--query_file", dest="query_file", default=None,
                    help="Execute the queries in the query file, delimited by ;")
  parser.add_option("-k", "--kerberos", dest="use_kerberos", default=False,
                    action="store_true", help="Connect to a kerberized impalad")
  parser.add_option("-s", "--kerberos_service_name",
                    dest="kerberos_service_name", default=None,
                    help="Service name of a kerberized impalad, default is 'impala'")
  parser.add_option("-V", "--verbose", dest="verbose", default=True, action="store_true",
                    help="Enable verbose output")
  parser.add_option("--quiet", dest="verbose", default=True, action="store_false",
                    help="Disable verbose output")
  parser.add_option("-v", "--version", dest="version", default=False, action="store_true",
                    help="Print version information")
  parser.add_option("-c", "--ignore_query_failure", dest="ignore_query_failure",
                    default=False, action="store_true", help="Continue on query failure")
  parser.add_option("-r", "--refresh_after_connect", dest="refresh_after_connect",
                    default=False, action="store_true",
                    help="Refresh Impala catalog after connecting")
  parser.add_option("-d", "--database", dest="default_db", default=None,
                    help="Issue a use database command on startup.")

  options, args = parser.parse_args()

  if options.version:
    print VERSION_STRING
    sys.exit(0)

  if options.use_kerberos:
    # The saslwrapper module has the same API as sasl, and is easier
    # to install on CentOS / RHEL. Look for saslwrapper first before
    # looking for the sasl module.
    try:
      import saslwrapper as sasl
    except ImportError:
      try:
        import sasl
      except ImportError:
        print 'Neither saslwrapper nor sasl module found'
        sys.exit(1)
    from thrift_sasl import TSaslClientTransport

    # The service name defaults to 'impala' if not specified by the user.
    if not options.kerberos_service_name:
      options.kerberos_service_name = 'impala'
    print "Using service name '%s' for kerberos" % options.kerberos_service_name
  elif options.kerberos_service_name:
    print 'Kerberos not enabled, ignoring service name'

  if options.query or options.query_file:
    execute_queries_non_interactive_mode(options)
    sys.exit(0)

  intro = WELCOME_STRING
  shell = ImpalaShell(options)
  while shell.is_alive:
    try:
      shell.cmdloop(intro)
    except KeyboardInterrupt:
      intro = '\n'
