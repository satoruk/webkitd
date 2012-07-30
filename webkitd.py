# -*- coding: utf-8 -*

from __future__ import with_statement

from pprint import pprint as p
import signal, sys, os, re, traceback, uuid, json, time, errno, logging, codecs, select, base64

from SocketServer import (ForkingTCPServer, BaseRequestHandler)

from PyQt4.QtCore import *
from PyQt4.QtGui import *
from PyQt4.QtWebKit import *
from PyQt4.QtNetwork import *


__version__ = '0.0.4'


sys.stdin  = codecs.getreader('utf-8')(sys.stdin)
sys.stdout = codecs.getwriter('utf-8')(sys.stdout)

logger = logging.getLogger('webkitd')
logger.setLevel(logging.DEBUG)

conerr = logging.StreamHandler(sys.stderr)
conerr.setLevel(logging.WARNING)
conerr_formatter = logging.Formatter('%(asctime)s pid:%(process)d tid:%(thread)d [%(levelname)s] %(message)s at %(module)s line:%(lineno)d')
conerr.setFormatter(conerr_formatter)
logger.addHandler(conerr)

console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.DEBUG)
console_formatter = logging.Formatter('%(asctime)s pid:%(process)d tid:%(thread)d [%(levelname)s] %(message)s at %(module)s line:%(lineno)d')
console.setFormatter(console_formatter)
logger.addHandler(console)


class WebKitRequestHandler(BaseRequestHandler):


  def __init__(self, request, client_address, server):
    self.logger = logging.getLogger('webkitd.WebKitRequestHandler')
    BaseRequestHandler.__init__(self, request, client_address, server)


  def handle(self):
    self.logger.info('new connection')

    app = QApplication([])
    app.setApplicationName(QString(u'WebKitServer'))
    app.setApplicationVersion(QString(__version__))

    socketDescriptor =  self.request.fileno()
    self.logger.debug('socketDescriptor:{0}'.format(socketDescriptor))

    socket = QTcpSocket(app)
    socket.setSocketDescriptor(socketDescriptor)
    worker = WebKitServer.Worker(socket, app, WebKitServer.Page)
    self.logger.debug('started worker!')
    app.exec_()
    self.logger.debug('app was exec')


  def finish(self):
    self.logger.debug('request close!')



class WebKitServer(ForkingTCPServer):


  @classmethod
  def walkObjectTree(cls, qobj, callback):
    children = map(lambda(c): cls.walkObjectTree(c, callback), qobj.children())
    return callback(qobj, children)


  @classmethod
  def getObjectTree(cls, qobj):
    def getObjectTreeInternal(qobj, children):
      return {
        u'type': unicode(qobj.metaObject().className()),
        u'children': children
      }
    return cls.walkObjectTree(qobj, getObjectTreeInternal)


  @classmethod
  def getObjectCount(cls, qobj):
    def getObjectCountInternal(qobj, children):
      return reduce(lambda x, y: x + y, children, 0) + 1
    return cls.walkObjectTree(qobj, getObjectCountInternal)


  @classmethod
  def getObjectCountMap(cls, qobj):
    countMap = {}
    def getObjectCountInternal(qobj, children):
      className = unicode(qobj.metaObject().className());
      if countMap.has_key(className):
        countMap[className] = countMap[className] + 1
      else:
        countMap[className] = 1
    cls.walkObjectTree(qobj, getObjectCountInternal)
    return countMap


  @classmethod
  def init(cls):
    signal.signal(signal.SIGINT, signal.SIG_DFL)


  @classmethod
  def start(cls, host='127.0.0.1', port=1982, maxclients=40):
    cls.init()
    cls.max_children = int(maxclients)
    server = cls((host, port), WebKitRequestHandler)
    server.serve_forever()


  @classmethod
  def daemon(cls, op='start', host=u'127.0.0.1', port=1982, pidPath=u'/tmp/webkitd.pid', stdout=u'/tmp/webkitd.out', stderr=u'/tmp/webkitd.err'):
    # daemon.pidlockfileが無かったらlockfile.pidlockfileを使う様に
    try:
      from daemon.pidlockfile import PIDLockFile
    except:
      from lockfile.pidlockfile import PIDLockFile

    pidFile = PIDLockFile(pidPath)

    def isOld(pidFile):
      old = False
      try:
        os.kill(pidFile.read_pid(), signal.SIG_DFL)
      except OSError, e:
        if e.errno == errno.ESRCH:
          old = True
      return old

    if op == u'start':
      if pidFile.is_locked():
        if isOld(pidFile):
          pidFile.break_lock()
        else:
          print u'Daemon is still started.'
          return

      from daemon import DaemonContext

      dc = DaemonContext(
        pidfile=pidFile,
        stderr=open(stderr, u'w+'),
        stdout=open(stdout, u'w+')
      )

      with dc:
        cls.start(host, port)

    elif op == u'stop':
      if not pidFile.is_locked():
        print u'Daemon is not started yet.'
        return

      if isOld(pidFile):
        pidFile.break_lock()
      else:
        os.kill(pidFile.read_pid(), signal.SIGINT)

    else:
      raise Exception(u'Unsupported daemon operation.')


  def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
    self.logger = logging.getLogger('webkitd.WebKitServer')
    ForkingTCPServer.__init__(self, server_address, RequestHandlerClass, bind_and_activate)
    self.logger.info('WebKitServer({0}) max children={1}'.format(server_address, self.max_children))

    def sigCHLD(num, frame):
      if num != signal.SIGCHLD: return
      self.logger.info('caught SIGCHLD {0}'.format(num))
      self.collect_children()
    signal.signal(signal.SIGCHLD, sigCHLD)
    signal.siginterrupt(signal.SIGCHLD, False)


    def process_request(self, request, client_address):
       self.logger.info('new connect')
       try:
         ForkingTCPServer.process_request(self, request, client_address)
       except OSError as e:
         if e.errno == errno.EAGAIN:
           self.logger.warning(str(e))
           request.sendall(json.dumps({'error': unicode(e.strerror), 'fatal': True, 'disconnect': True }, ensure_ascii=False, encoding=u'UTF-8') + u'\n')
           request.close()
         else:
           raise

  def serve_forever(self, poll_interval=0.2):
    # SocketServer 0.4 doesn't handle syscall interruption
    # http://bugs.python.org/issue7978
    self._BaseServer__is_shut_down.clear()
    try:
      while not self._BaseServer__shutdown_request:
        r, w, e = self.__eintr_retry(select.select, [self], [], [], poll_interval)
        if self in r:
          self._handle_request_noblock()
    finally:
      self._BaseServer__shutdown_request = False
      self._BaseServer__is_shut_down.set()


  def __eintr_retry(self, func, *args):
    """restart a system call interrupted by EINTR"""
    while True:
      try:
        return func(*args)
      except (OSError, IOError, select.error) as e:
        if e.args[0] != errno.EINTR:
          raise



class WebKitWorker(QObject):


  jobClassMap = {}


  def __init__(self, socket, app, Page):
    self.logger = logging.getLogger('webkitd.WebKitWorker')
    self.logger.info('New worker remote={0}:{1}, local={2}:{3}'.format(socket.peerAddress().toString(), socket.peerPort(), socket.localAddress().toString(), socket.localPort()))
    QObject.__init__(self, app)
    self.app = app
    self.Page = Page
    self.page = Page(app)
    self.socket = socket
    self.isBusy = False
    self.page.javascriptInterrupted.connect(self.handleJavaScriptInterrupted)
    socket.disconnected.connect(self.page.deleteLater)
    socket.disconnected.connect(self.deleteLater)
    socket.readyRead.connect(self.handleReadyRead)
    socket.stateChanged.connect(self.stateChanged)


  def stateChanged(self, socketState):
    if QAbstractSocket.UnconnectedState==socketState:
      self.logger.info('UnconnectedState')

      try:
        self.socket.stateChanged.disconnect(self.stateChanged)
      except:
        pass

      try:
        self.socket.readyRead.disconnect(self.handleReadyRead)
      except:
        pass

      self.socket = None
      self.page.settings().clearMemoryCaches()
      self.page = None
      self.app.exit(0)


  def handleJavaScriptInterrupted(self):
    self.fatalAndDisconnect(Exception(u'Heavy JavaScript interrupted!'))


  def sendData(self, data):
    self.socket.write((data + u'\n').encode(u'UTF-8'))
    self.socket.flush()


  def handleReadyRead(self):
    self.logger.info(u'Handle Ready Read')
    while self.socket and self.socket.canReadLine():
      # For UTF-8 Text Protocol
      # QByteArray -> Python String -> Python Unicode
      line = self.socket.readLine();
      data = str(line).decode(u'UTF-8').strip()
      self.logger.debug(u'Data arrived: ' + data)
      self.emitData(data)


  def emitData(self, data):
    try:
      if not self.isBusy:
        self.isBusy = True
        try:
          data = json.loads(data, encoding=u'UTF-8')
        except ValueError, e:
          raise WebKitProtocolViolationException(e)

        if not data.has_key(u'type') or not data.has_key(u'data'):
          raise WebKitProtocolViolationException(u'Invalid sended data. Data must has \'type\' and \'data\' key.')

        t = data[u'type']

        if not self.jobClassMap.has_key(t):
          raise WebKitProtocolViolationException(u'Invalid sended data. Invalid job: ' + t)

        Job = self.__class__.jobClassMap[t]
        try:
          job = Job(self, self.page, data[u'data'])
        except Exception, e:
          raise Exception(u'Can\'t create job for ' + t + u'.')
          
        job.start()

      else:
        raise WebKitProtocolViolationException(u'WebKit worker is still processing.')

    except Exception, e:
      traceback.print_exc();
      self.fatal(e)


  def fatalAndDisconnect(self, e):
    self.sendData(json.dumps({ u'error': unicode(e), u'fatal': True, u'disconnect': True }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False
    self.disconnect()


  def fatal(self, e):
    self.sendData(json.dumps({ u'error': unicode(e), u'fatal': True }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False


  def error(self, e):
    self.sendData(json.dumps({ u'error': unicode(e) }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False


  def finish(self, data):
    self.sendData(json.dumps({ u'data': data }, ensure_ascii=False, encoding=u'UTF-8'))
    self.isBusy = False


  def disconnect(self):
    self.logger.info(u'Worker destroyed.')
    self.socket.readyRead.disconnect(self.handleReadyRead)
    self.socket.disconnectFromHost()


  @classmethod
  def appendJobClass(cls, JobClass):
    cls.jobClassMap[JobClass.id] = JobClass



class WebKitLoadUrlJob():


  id = u'load-url'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitLoadUrlJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.url = data[u'url']


  def start(self):
    try:
      self.page.navigate(self.url, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, ok, status, timeouted):
    try:
      title = unicode(self.page.mainFrame().findFirstElement(u'title').toPlainText())
      finalUrl = unicode(self.page.mainFrame().url().toString())
      url = unicode(self.page.mainFrame().requestedUrl().toString())
      self.worker.finish({ u'ok': ok, u'status': status, u'timeouted': timeouted, u'title': title, u'finalUrl': finalUrl, u'url': url })
    except Exception, e:
      traceback.print_exc()
      self.worker.error(e)



class WebKitCheckElementJob():


  id = u'check-element'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitCheckElementJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']


  def start(self):
    try:
      self.page.checkElement(self.xpath, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, timeouted, error):
    try:
      self.worker.finish({ u'found': found, u'count': count, u'error': error, u'timeouted': timeouted})
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitClickElementJob(WebKitCheckElementJob):


  id = u'click-element'


  def start(self):
    try:
      self.page.click(self.xpath, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitWaitElementJob(WebKitCheckElementJob):


  id = u'wait-element'


  def start(self):
    self.logger.info('start')
    self.startTime = time.time()
    self.check()


  def check(self):
    self.logger.info('check')
    try:
      timeout =  self.startTime + self.timeout - time.time()
      if timeout < 0:
        self.worker.finish({ u'found': False, u'count': 0, u'error': u'Timeouted!', u'timeouted': True})
      else:
        self.page.checkElement(self.xpath, timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, timeouted, error):
    self.logger.info('callback')
    try:
      if (found or error):
        self.worker.finish({ u'found': found, u'count': count, u'error': error, u'timeouted': timeouted})
      else:
        self.page.timeout(0.2, self.check)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


class WebKitScrollElementJob():


  id = u'scroll-element'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitScrollElementJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.scroll = data[u'scroll']


  def start(self):
    try:
      self.page.scrollElement(self.xpath, self.scroll, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, beforeTop, afterTop, scrollHeight, timeouted, error):
    try:
      self.worker.finish({
        u'found': found,
        u'count': count,
        u'beforeTop': beforeTop,
        u'afterTop': afterTop,
        u'scrollHeight': scrollHeight,
        u'timeouted': timeouted,
        u'error': error
      })
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitGetElementValueJob():


  id = u'get-element-value'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitGetElementValueJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.attr = data[u'attr']


  def start(self):
    try:
      self.page.getElementValue(self.xpath, self.attr, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, hasProp, propValue, hasAttr, attrValue, timeouted, error):
    try:
      self.worker.finish({
        u'found': found,
        u'count': count,
        u'hasProp': hasProp,
        u'propValue': propValue,
        u'hasAttr': hasAttr,
        u'attrValue': attrValue,
        u'timeouted': timeouted,
        u'error': error,
      })
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitSetElementAttrJob(WebKitCheckElementJob):


  id = u'set-element-attr'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitSetElementAttrJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.attr = data[u'attr']
    self.value = data[u'value']


  def start(self):
    try:
      self.page.setElementAttribute(self.xpath, self.attr, self.value, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


class WebKitSetElementPropJob(WebKitCheckElementJob):


  id = u'set-element-prop'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitSetElementPropJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.prop = data[u'prop']
    self.value = data[u'value']


  def start(self):
    try:
      self.page.setElementProperty(self.xpath, self.prop, self.value, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


class WebKitCallElementMethodJob():


  id = u'check-element-method'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitCallElementMethodJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.xpath = data[u'xpath']
    self.prop = data[u'prop']
    self.args = data[u'args']


  def start(self):
    try:
      self.page.callElementMethod(self.xpath, self.prop, self.args, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, found, count, resultList, timeouted, error):
    try:
      self.worker.finish({ u'found': found, u'count': count, u'resultList': resultList, u'error': error, u'timeouted': timeouted})
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitEvalStringJob():


  id = u'eval-string'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitEvalStringJob')
    self.worker = worker
    self.page = page;
    self.timeout = data[u'timeout']
    self.code = data[u'code']


  def start(self):
    try:
      self.page.evalString(self.code, self.timeout, self.callback)
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def callback(self, result, timeouted, error):
    try:
      self.worker.finish({ u'result': result, u'error': error, u'timeouted': timeouted})
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)



class WebKitServerJob():


  id = u'server'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitServerJob')
    self.data = data
    self.worker = worker
    self.page = page;


  def start(self):
    try:
      if (self.data[u'command'] == u'disconnect'):
        self.worker.finish({ u'message': 'bye-bye' })
        self.worker.disconnect()

      elif (self.data[u'command'] == u'to-plain-text'):
        value = unicode(self.page.mainFrame().toPlainText())
        self.worker.finish({ u'to-plain-text': 'to-plain-text', u'value': value })

      elif (self.data[u'command'] == u'to-html'):
        value = unicode(self.page.mainFrame().toHtml())
        self.worker.finish({ u'to-html': 'to-html', u'value': value })

      elif (self.data[u'command'] == u'grab'):
        value = self.grab()
        self.worker.finish({ u'grab': 'grab', u'value': value })

      elif (self.data[u'command'] == u'status'):
        objectCount = WebKitServer.getObjectCount(self.worker.app)
        objectCountMap = WebKitServer.getObjectCountMap(self.worker.app)

        status = self.page.status()
        self.worker.finish({
          u'objectCount': objectCount,
          u'objectCountMap': objectCountMap,
          u'title': status[u'title'],
          u'url': status[u'url'],
          u'requestedUrl': status[u'requestedUrl'],
          u'totalBytes': status[u'totalBytes'],
          u'width': status[u'width'],
          u'height': status[u'height'],
          u'selectedText': status[u'selectedText']
        })

      else:
        self.worker.finish({ u'error': 'command not found' })

    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def grab(self):
    self.logger.info('grab')

    view = self.page.view()
    #filename = u'/tmp/WebKitd_grab_{0}.png'.format(str(uuid.uuid1()))
    pixmap = QPixmap.grabWidget(view)
    #ret = pixmap.save(QString(filename), unicode('png'))

    byteArray = QByteArray()
    buf = QBuffer(byteArray);
    buf.open(QIODevice.WriteOnly);
    ret = pixmap.save(buf, unicode('png'))

    #b64val = base64.encodestring(byteArray)
    b64val = str(byteArray.toBase64())

    self.logger.info('grab ... {0}'.format(ret))
    if (ret): return b64val
    return False



#
# proxy on
# { "type": "preference", "data": { "name" : "proxy", "value" : { "host" : "", "port" : 8080 } } }
#
# proxy off
# { "type": "preference", "data": { "name" : "proxy", "value" : { } } }
#
# user agent
# { "type": "preference", "data": { "name" : "userAgent", "value" : "" } }
#
class WebKitPreferenceJob():


  id = u'preference'


  def __init__(self, worker, page, data):
    self.logger = logging.getLogger('webkitd.WebKitPreferenceJob')
    self.worker = worker
    self.page = page
    self.data = data


  def start(self):
    try:
      propName = self.data[u'name']
      propValue = self.data[u'value']
      if (propName == u'userAgent'):
        self.setUserAgent(propValue)
        self.worker.finish({ propName : True })
      elif (propName == u'proxy'):
        self.setProxy(propValue)
        self.worker.finish({ propName : True })
      else:
        self.worker.finish({ u'error': u'property not found' })
    except Exception, e:
      traceback.print_exc();
      self.worker.error(e)


  def setUserAgent(self, value):
    self.page.userAgent = value


  def setProxy(self, value):
    proxy = QNetworkProxy()
    if value.has_key(u'host'):
      proxy.setType(QNetworkProxy.HttpProxy)
      proxy.setHostName(value[u'host'])
      proxy.setPort(int(str(value[u'port']), 10))
    else:
      proxy.setType(QNetworkProxy.NoProxy)
    self.page.networkAccessManager().setProxy(proxy)



class WebKitPage(QWebPage):

  javascriptInterrupted = pyqtSignal()

  ignoreNetowrkReplys = (
    QNetworkReply.NoError,
    QNetworkReply.AuthenticationRequiredError,       # 401 Authorization required
    QNetworkReply.ContentOperationNotPermittedError, # 403 Access denied
    QNetworkReply.ContentNotFoundError,              # 404 Not Found
    QNetworkReply.ContentOperationNotPermittedError, # 405 Method Not Allowed
    QNetworkReply.ProxyAuthenticationRequiredError,  # 407 Proxy Authentication Required
    QNetworkReply.ProtocolUnknownError,              # 5xx
    QNetworkReply.UnknownContentError,               # 4xx
  )


  def __init__(self, parent):
    self.logger = logging.getLogger('webkitd.WebKitPage')
    QWebPage.__init__(self, parent)

    self.userAgent = None

    self.timer = QTimer()
    self.timer.isUsed = False
    self.timer.setSingleShot(True)

    settings = {
      QWebSettings.AutoLoadImages: False,
      QWebSettings.JavascriptCanOpenWindows: False,
      QWebSettings.JavaEnabled: False,
      QWebSettings.PluginsEnabled: False,
      QWebSettings.PrivateBrowsingEnabled: True,
      QWebSettings.DnsPrefetchEnabled: True,
    }
    for k, v in settings.items():
      self.settings().setAttribute(k, v)

    self.loadProgress.connect(self.handleLoadProgress)
    self.loadStarted.connect(self.handleLoadStarted)
    self.loadFinished.connect(self.handleLoadFinished)
    self.networkAccessManager().finished.connect(self.handleResourceLoadFinished)
    self.mainFrame().initialLayoutCompleted.connect(self.handleInitialLayoutCompleted)
    self.mainFrame().javaScriptWindowObjectCleared.connect(self.handleJavaScriptWindowObjectCleared)
    self.mainFrame().urlChanged.connect(self.handleUrlChanged)
    self.mainFrame().titleChanged.connect(self.handleTitleChanged)
    # self.mainFrame().pageChanged.connect(self.handlePageChanged)
    # qt 4:4.7.0-0ubuntu4.3 (or python-qt4) does not have PageChange signal
    if hasattr(self.mainFrame(), 'pageChanged'):
      self.mainFrame().pageChanged.connect(self.handlePageChanged)

    view = QWebView()
    view.setPage(self)
    self.setView(view)
    view.showFullScreen()


  def status(self):
    return {
      u'title': unicode(self.mainFrame().findFirstElement(u'title').toPlainText()),
      u'url': unicode(self.mainFrame().url().toString()),
      u'requestedUrl': unicode(self.mainFrame().requestedUrl().toString()),
      u'totalBytes': self.totalBytes(),
      u'width': self.mainFrame().contentsSize().width(),
      u'height': self.mainFrame().contentsSize().height(),
      u'selectedText': unicode(self.selectedText()),
    }


  def handleInitialLayoutCompleted(self):
    self.logger.info(u'Handle initial layout completed.')


  def handleInitialLayoutCompleted(self):
    self.logger.info(u'Handle initial layout completed.')


  def handleJavaScriptWindowObjectCleared(self):
    self.logger.info(u'JavaScript window object cleared.')


  def handleTitleChanged(self, title):
    self.logger.info(u'Title changed: ' + unicode(title))


  def handleUrlChanged(self, url):
    self.logger.info(u'Url changed: ' + unicode(url.toString()))


  def handlePageChanged(self):
    self.logger.info(u'Page changed.')


  def handleLoadProgress(self, progress):
    self.logger.info(u'Loading... ' + str(progress) + u'%')


  def handleLoadStarted(self):
    self.logger.info(u'Load started.')


  def handleLoadFinished(self, ok):
    self.logger.info(u'Load finished.')

  def handleResourceLoadFinished(self, reply):
    err = reply.error()
    if ( err in self.ignoreNetowrkReplys ):
      return
    msg = u'Resource loaded: {0}, reply.error:{1}'.format(reply.url().toString(), reply.error())
    if ( err == QNetworkReply.OperationCanceledError ):
      self.logger.info(msg + u' OperationCanceledError')
    else:
      self.logger.warning(msg)

  def timeout(self, time, callback):
    self.logger.info(u'Waiting: {0} sec'.format(unicode(time)))
    if (self.timer.isUsed):
      raise Exception(u'Timer is used')
    self.timer.isUsed = True
    self.timer.start(int(time * 1000))

    def handleTimeout():
      self.logger.info(u'Load timeout.')
      self.timer.stop()
      self.timer.timeout.disconnect(handleTimeout)
      if (self.timer.isUsed):
        self.timer.isUsed = False
        callback()
 
    self.timer.timeout.connect(handleTimeout)


  def cancelTimeout(self):
    self.logger.info('cancelTimeout')
    self.timer.isUsed = False
    self.timer.timeout.emit()


  def navigate(self, url, timeout, callback):
    self.logger.info(u"Preparing to load... url:{0} timeout:{1} ".format(url, timeout))

    context = {
      u'status': 0,
      u'timeouted': False,
      u'requestArrived': False,
      u'nam': self.networkAccessManager()
    }

    def handleResourceLoadFinished(reply):
      self.logger.info('handleResourceLoadFinished')
      if (context[u'timeouted']):
        raise Exception(u'Timeout and load finished occurred both. This is bug.')
      if (unicode(self.mainFrame().url().toString()) == unicode(reply.url().toString())):
        context[u'status'] = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute).toInt()[0]

    def handleLoadFinished(ok):
      self.logger.info('handleLoadFinished')
      self.cancelTimeout()
      self.loadFinished.disconnect(handleLoadFinished)
      context[u'nam'].finished.disconnect(handleResourceLoadFinished)
      context[u'requestArrived'] = True
      if (context[u'timeouted']):
        raise Exception(u'Timeout and load finished occurred both. This is bug.')
      callback(ok, context[u'status'], context[u'timeouted'])

    def handleTimeout():
      self.logger.info('handleTimeout')
      self.cancelTimeout()
      context[u'timeouted'] = True
      context[u'nam'].finished.disconnect(handleResourceLoadFinished)
      self.loadFinished.disconnect(handleLoadFinished)
      if (context[u'requestArrived']):
        raise Exception(u'Timeout and load finished occurred both. This is bug.')
      callback(False, context[u'status'], context[u'timeouted'])

    context[u'nam'].finished.connect(handleResourceLoadFinished)
    self.loadFinished.connect(handleLoadFinished)
    self.timeout(timeout, handleTimeout)
    self.mainFrame().load(QUrl(url))

  def click(self, xpath, timeout, callback):
    self.logger.info(u'Preparing to click...: ' + xpath)
    js = '''
      function(xpath) {
        var elList = __xpath__(xpath);
        elList.forEach(__click__);
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [xpath], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def setElementAttribute(self, xpath, attr, value, timeout, callback):
    self.logger.info(u'Preparing to set element attribute...: ' + xpath + u' (' + attr + u')')
    js = '''
      function(xpath, attr, value) {
        var elList = __xpath__(xpath);
        elList.forEach(__setAttr__(attr, value))
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [xpath, attr, value], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def setElementProperty(self, xpath, prop, value, timeout, callback):
    self.logger.info(u'Preparing to set element property...: ' + xpath + u' (' + prop + u')')
    js = '''
      function(xpath, prop, value) {
        var elList = __xpath__(xpath);
        elList.forEach(__setProp__(prop, value))
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [xpath, prop, value], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def callElementMethod(self, xpath, prop, args, timeout, callback):
    self.logger.info(u'Preparing to call element method...: ' + xpath + u' (' + prop + u')')
    js = '''
      function(xpath, prop, args) {
        var elList = __xpath__(xpath);
        var resultList = elList.map(__callProp__(prop, args))
        return [!!elList.length, elList.length, resultList]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    resultList = []
    try:
      (found, count, resultList) = self.js(js, [xpath, prop, args], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, resultList, timeouted, error)


  def evalString(self, code, timeout, callback):
    self.logger.info(u'Preparing to evaluate string...: (' + code + u')')
    js = '''
      function(code) {
        return eval(code)
      }
    '''
    result = False
    timeouted = False
    error = False
    try:
      result = self.js(js, [code], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(result, timeouted, error)


  def getElementValue(self, xpath, attr, timeout, callback):
    self.logger.info(u'Preparing to get element value...: ' + xpath + u' (' + attr + u')')
    js = '''
      function(xpath, attr) {
        var elList = __xpath__(xpath);
        var el = elList[0];
        if (el) {
          var hasProp = attr in el;
          var propValue = el[attr];
          var hasAttr =  el.hasAttribute(attr);
          var attrValue = el.getAttribute(attr);
        }
        else {
          var hasProp = false;
          var propValue = void(0);
          var hasAttr = false;
          var attrValue = void(0);
        }
        return [!!elList.length, elList.length, hasProp, propValue, hasAttr, attrValue]
      }
    '''
    found = False
    count = 0
    hasProp = False
    propValue = None
    hasAttr = False
    attrValue = None
    timeouted = False
    error = False
    try:
      (found, count, hasProp, propValue, hasAttr, attrValue) = self.js(js, [xpath, attr], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, hasProp, propValue, hasAttr, attrValue, timeouted, error)


  def scrollElement(self, xpath, scroll, timeout, callback):
    self.logger.info(u'Preparing to scroll...: ' + xpath + u' (' + unicode(scroll) + u')')
    js = '''
      function(xpath, scroll) {
        var elList = __xpath__(xpath);
        var el = elList[0];
        if (el) {
          var r = __scroll__(scroll)(el)
          var beforeTop = r[0];
          var afterTop = r[1];
          var scrollHeight = r[2];
        }
        else {
          var beforeTop = 0, afterTop = 0, scrollHeight = 0;
        }
        return [!!elList.length, elList.length, beforeTop, afterTop, scrollHeight]
      }
    '''
    found = False
    count = 0
    beforeTop = 0
    afterTop = 0
    scrollHeight = 0
    timeouted = False
    error = False
    try:
      (found, count, beforeTop, afterTop, scrollHeight) = self.js(js, [xpath, scroll], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, beforeTop, afterTop, scrollHeight, timeouted, error)


  def checkElement(self, xpath, timeout, callback):
    self.logger.info(u'Preparing to check...: ' + xpath)
    js = '''
      function(xpath) {
        var elList = __xpath__(xpath);
        return [!!elList.length, elList.length]
      }
    '''
    found = False
    count = 0
    timeouted = False
    error = False
    try:
      (found, count) = self.js(js, [xpath], timeout)
    except WebKitJavaScriptTimeoutException, e:
      timeouted = True
      error = traceback.format_exception_only(type(e), e)
    except WebKitJavaScriptErrorException, e:
      timeouted = False
      error = traceback.format_exception_only(type(e), e)
    callback(found, count, timeouted, error)


  def js(self, js, args, timeout):
    timer = QTimer()
    def handleTimeout():
      self.logger.info("Heavy JavaScript interrupted!")
      self.javascriptInterrupted.emit()
    timer.timeout.connect(handleTimeout)
    timer.setSingleShot(True)
    timer.start(1)
    frame = self.mainFrame()
    result = frame.evaluateJavaScript(u'''(function() {
      var __args__ = ''' + json.dumps(args, ensure_ascii=False, encoding=u'UTF-8') + u''';
      var __timeout__ = ''' + unicode(timeout * 1000) + u''';
      var __startTime__ = new Date().getTime();
      var __logs__ = [];
      var __isTimeouted__ = false;
      function __click__(element) {
        __checkTimeout__();
        var e = document.createEvent('MouseEvents');
        __checkTimeout__();
        e.initMouseEvent("click", true, true, window,
            0, 0, 0, 0, 0, false, false, false, false, 0, null);
        __checkTimeout__();
        var r = element.dispatchEvent(e);
        __checkTimeout__();
        return r;
      }
      function __callProp__(prop, args) {
        __checkTimeout__();
        return function(obj) {
          __checkTimeout__();
          return obj[prop].apply(obj, args)
        }
      }
      function __setAttr__(attr, value) {
        __checkTimeout__();
        return function(element) {
          __checkTimeout__();
          element.setAttribute(attr, value)
        }
      }
      function __setProp__(prop, value) {
        __checkTimeout__();
        return function(obj) {
          __checkTimeout__();
          obj[prop] = value;
        }
      }
      function __scroll__(scroll) {
        __checkTimeout__();
        return function(element) {
          __checkTimeout__();
          var beforeTop = element.scrollTop;
          __checkTimeout__();
          element.scrollTop = scroll;
          __checkTimeout__();
          var afterTop = element.scrollTop;
          __checkTimeout__();
          var scrollHeight = element.scrollHeight;
          __checkTimeout__();
          return [beforeTop, afterTop, scrollHeight];
        }
      }
      function __log__(message) {
        __checkTimeout__();
        __logs__.push(message.toString());
      }
      function __xpath__(xpath, context, fn) {
        __checkTimeout__();
        context = context || document;
        __checkTimeout__();
        fn = fn || function(node) { return node };
        __checkTimeout__();
        var r = document.evaluate(xpath, context, null, 7, null);
        __checkTimeout__();
        var list = [];
        __checkTimeout__();
        for (var i = 0; i < r.snapshotLength; i++) {
          __checkTimeout__();
          var result = fn(r.snapshotItem(i))
          __checkTimeout__();
          list.push(result);
        }
        __checkTimeout__();
        var resultList = list.filter(function(e) { return e });
        __checkTimeout__();
        return resultList;
      }
      function __checkTimeout__() {
        if (new Date().getTime() - __startTime__ > __timeout__) {
          __isTimeouted__ = true;
          throw Error('Timeout in JavaScript.')
        }
      }
      // for webkit evaluateJavaScript problem
      function __cleanUpNull__(data) {
        for (name in data) {
          if (data[name] === null || data[name] === void(0)) {
            data[name] = false;
            if (typeof (data[name]) === 'object' && data[name] !== null) {
              __cleanUpNull__(data[name]);
            }
          }
        }
      }
      try {
        var __result__ = (''' + js + u''' ).apply(null, __args__)
        __cleanUpNull__(__result__)
        return { data: __result__, timeouted: __isTimeouted__, logs: __logs__ };
      }
      catch (e) {
        return { error: e.message, timeouted: __isTimeouted__, logs: __logs__ };
      }
    })()''')
    timer.stop()
    result = self.toPythonValue(result)
    for log in result[u'logs']:
      self.logger.info(u'JavaScript Log: ' + log)
    if result.has_key(u'error') and result[u'timeouted']:
      raise WebKitJavaScriptTimeoutException(result[u'error'])
    if result.has_key(u'error') and not result[u'timeouted']:
      raise WebKitJavaScriptErrorException(result[u'error'])
    if result[u'timeouted']:
      raise Exception(u'Timeout and JavaScript success is occurred both. This is bug.')
    if not result.has_key(u'data'):
      raise Exception(u'JavaScript result dosen\'t have data. This is bug.')


    return result[u'data']


  def toPythonValue(self, qtValue):
    if (qtValue.typeName() == u'QString'):
      value = unicode(qtValue.toString())
    elif (qtValue.typeName() == u'double'):
      value = qtValue.toDouble()[0]
    elif (qtValue.typeName() == u'bool'):
      value = qtValue.toBool()
    elif (qtValue.typeName() == u'QVariantList'):
      value = []
      for v in qtValue.toList():
        value.append(self.toPythonValue(v))
    elif (qtValue.typeName() == u'QVariantMap'):
      value = {}
      qtMap = qtValue.toMap()
      for k, v in qtMap.items():
        value[unicode(k)] = self.toPythonValue(v)
    else:
      raise Exception(u'Unsupported QtType: ' + qtValue.typeName())
    return value


  # XXX 現状の libwebkit ではこれは non-virtual なため効かない
  @pyqtSlot(bool)
  def shouldInterruptJavascript():
    return False


  def chooseFile(self, frame, suggestedFile):
    return u''


  def javaScriptAlert(self, frame, msg):
    pass


  def javaScriptPrompt(self, frame, msg):
    return True


  def javaScriptConfirm(self, frame, msg):
    return True


  def javaScriptConsoleMessage(self, msg, lineNumber, sourceId):
    pass


  def userAgentForUrl(self, url):
    if self.userAgent == None :
      return super(self.__class__, self).userAgentForUrl(url)
    return self.userAgent


  def supportsExtension(self, extension):
    return extension == QWebPage.ErrorPageExtension


  def extension(self, extension, option, output):
    if (extension == QWebPage.ErrorPageExtension):
      self.logger.warning(u'Page error has ocurred!')
    return True



class WebKitProtocolViolationException(Exception):
  pass

class WebKitJavaScriptTimeoutException(Exception):
  pass

class WebKitJavaScriptErrorException(Exception):
  pass



WebKitServer.Worker = WebKitWorker
WebKitServer.Page = WebKitPage

WebKitWorker.appendJobClass(WebKitLoadUrlJob)
WebKitWorker.appendJobClass(WebKitServerJob)
WebKitWorker.appendJobClass(WebKitPreferenceJob)
WebKitWorker.appendJobClass(WebKitClickElementJob)
WebKitWorker.appendJobClass(WebKitCheckElementJob)
WebKitWorker.appendJobClass(WebKitWaitElementJob)
WebKitWorker.appendJobClass(WebKitScrollElementJob)
WebKitWorker.appendJobClass(WebKitGetElementValueJob)
WebKitWorker.appendJobClass(WebKitSetElementAttrJob)
WebKitWorker.appendJobClass(WebKitSetElementPropJob)
WebKitWorker.appendJobClass(WebKitCallElementMethodJob)
WebKitWorker.appendJobClass(WebKitEvalStringJob)

if __name__ == "__main__":

  import argparse
  parser = argparse.ArgumentParser(prog='WebKit Server', usage='%(prog)s [options]', description=u'Simple WebKit Server')
  parser.add_argument('--version', action='version', version='%(prog)s {0}'.format(__version__))
  subparsers = parser.add_subparsers(help='operation')
  startparser = subparsers.add_parser('start', help='Start server')
  startparser.add_argument(u'--host', default='127.0.0.1')
  startparser.add_argument(u'--port', type=int, default=1982)
  startparser.add_argument(u'--pidfile', default='/tmp/webkitd.pid')
  startparser.add_argument(u'--stdout', default='/tmp/webkitd.out')
  startparser.add_argument(u'--stderr', default='/tmp/webkitd.err')
  startparser.add_argument(u'--maxclients', default=40)
  startparser.add_argument(u'foreground')
  stopparser = subparsers.add_parser('stop', help='Stop server')
  stopparser.add_argument(u'--host', default='127.0.0.1')
  stopparser.add_argument(u'--port', type=int, default=1982)
  stopparser.add_argument(u'--pidfile', default='/tmp/webkitd.pid')
  stopparser.add_argument(u'--stdout', default='/tmp/webkitd.out')
  stopparser.add_argument(u'--stderr', default='/tmp/webkitd.err')

  args = parser.parse_args()

  if sys.argv[1] == 'start':
    if args.foreground == 'foreground':
      WebKitServer.start(host=args.host, port=args.port, maxclients=args.maxclients)
    else:
      WebKitServer.daemon(sys.argv[1], args.host, args.port, args.pidfile, args.stdout, args.stderr)
  else:
    WebKitServer.daemon(sys.argv[1], args.host, args.port, args.pidfile, args.stdout, args.stderr)

