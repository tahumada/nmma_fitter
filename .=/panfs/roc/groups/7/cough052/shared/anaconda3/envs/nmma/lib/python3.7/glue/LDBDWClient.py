"""
The LDBDWClient module provides an API for connecting to and making requests of a LDBDWServer.

This file is part of the Grid LSC User Environment (GLUE)

GLUE is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details.

You should have received a copy of the GNU General Public License along with
this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from glue import git_version
__date__ = git_version.date
__version__ = git_version.id

import sys
import os
import types
import re
import six.moves.cPickle
import xml.parsers.expat
import six.moves.http_client
import calendar
import time


try:
    from cjson import (decode, encode)
except ImportError:
    from json import (loads as decode, dumps as encode)

from OpenSSL import crypto

def version():
  return __version__


class SimpleLWXMLParser:
  """
  A very simple LIGO_LW XML parser class that reads the only keeps
  tables that do not contain the strings sngl_ or multi_

  The class is not very robust as can have problems if the line
  breaks do not appear in the standard places in the XML file.
  """
  def __init__(self):
    """
    Constructs an instance.

    The private variable ignore_pat determines what tables we ignore.
    """
    self.__p = xml.parsers.expat.ParserCreate()
    self.__in_table = 0
    self.__silent = 0
    self.__ignore_pat = re.compile(r'.*(sngl_|multi_).*', re.IGNORECASE)
    self.__p.StartElementHandler = self.start_element
    self.__p.EndElementHandler = self.end_element

  def __del__(self):
    """
    Destroys an instance by shutting down and deleting the parser.
    """
    self.__p("",1)
    del self.__p

  def start_element(self, name, attrs):
    """
    Callback for start of an XML element. Checks to see if we are
    about to start a table that matches the ignore pattern.

    @param name: the name of the tag being opened
    @type name: string

    @param attrs: a dictionary of the attributes for the tag being opened
    @type attrs: dictionary
    """
    if name.lower() == "table":
      for attr in attrs.keys():
        if attr.lower() == "name":
          if self.__ignore_pat.search(attrs[attr]):
            self.__in_table = 1
        
  def end_element(self, name):
    """
    Callback for the end of an XML element. If the ignore flag is
    set, reset it so we start outputing the table again.

    @param name: the name of the tag being closed
    @type name: string
    """
    if name.lower() == "table":
      if self.__in_table:
        self.__in_table = 0

  def parse_line(self, line):
    """
    For each line we are passed, call the XML parser. Returns the
    line if we are outside one of the ignored tables, otherwise
    returns the empty string.

    @param line: the line of the LIGO_LW XML file to be parsed
    @type line: string

    @return: the line of XML passed in or the null string
    @rtype: string
    """
    self.__p.Parse(line)
    if self.__in_table:
      self.__silent = 1
    if not self.__silent:
      ret = line
    else:
      ret = ""
    if not self.__in_table:
      self.__silent = 0
    return ret

def findCredential():
    """
    Follow the usual path that GSI libraries would
    follow to find a valid proxy credential but
    also allow an end entity certificate to be used
    along with an unencrypted private key if they
    are pointed to by X509_USER_CERT and X509_USER_KEY
    since we expect this will be the output from
    the eventual ligo-login wrapper around
    kinit and then myproxy-login.
    """

    # use X509_USER_PROXY from environment if set
    if 'X509_USER_PROXY' in os.environ:
        filePath = os.environ['X509_USER_PROXY']
        if validateProxy(filePath):
            return filePath, filePath
        else:
            RFCproxyUsage()
            sys.exit(1)

    # use X509_USER_CERT and X509_USER_KEY if set
    if 'X509_USER_CERT' in os.environ:
        if 'X509_USER_KEY' in os.environ:
            certFile = os.environ['X509_USER_CERT']
            keyFile = os.environ['X509_USER_KEY']
            return certFile, keyFile

    # search for proxy file on disk
    uid = os.getuid()
    path = "/tmp/x509up_u%d" % uid

    if os.access(path, os.R_OK):
        if validateProxy(path):
            return path, path
        else:
            RFCproxyUsage()
            sys.exit(1)

    # if we get here could not find a credential
    RFCproxyUsage()
    sys.exit(1)

def validateProxy(path):
    """Validate the users X509 proxy certificate

    Tests that the proxy certificate is RFC 3820 compliant and that it
    is valid for at least the next 15 minutes.

    @returns: L{True} if the certificate validates
    @raises RuntimeError: if the certificate cannot be validated
    """
    # load the proxy from path
    try:
        with open(path, 'rt') as f:
            cert = crypto.load_certificate(crypto.FILETYPE_PEM, f.read())
    except IOError as e:
        e.args = ('Failed to load proxy certificate: %s' % str(e),)
        raise

    # try and read proxyCertInfo
    rfc3820 = False
    for i in range(cert.get_extension_count()):
        if cert.get_extension(i).get_short_name() == 'proxyCertInfo':
            rfc3820 = True
            break

    # otherwise test common name
    if not rfc3820:
        subject = cert.get_subject()
        if subject.CN.startswith('proxy'):
            raise RuntimeError('Could not find a valid proxy credential')

    # check time remaining
    expiry = cert.get_notAfter()
    if isinstance(expiry, bytes):
        expiry = expiry.decode('utf-8')
    expiryu = calendar.timegm(time.strptime(expiry, "%Y%m%d%H%M%SZ"))
    if expiryu < time.time():
        raise RuntimeError('Required proxy credential has expired')

    # return True to indicate validated proxy
    return True

    
 

def RFCproxyUsage():
    """
    Print a simple error message about not finding
    a RFC 3820 compliant proxy certificate.
    """
    msg = """\
Could not find a valid proxy credential.
LIGO users, please run 'ligo-proxy-init' and try again.
Others, please run 'grid-proxy-init' and try again.
"""

    sys.stderr.write(msg)


class LDBDClientException(Exception):
  """Exceptions returned by server"""
  pass


class LDBDClient(object):
  def __init__(self, host, port = None, protocol = None, identity = None):
    """
    """
    self.host = host
    self.port = port
    self.protocol = protocol

    if self.port:
        self.server = "%s:%d" % (self.host, self.port)
    else:
        self.server = self.host

    # this is a holdover from the pyGlobus era and is not
    # acutally used at this time since we do not check the
    # credential of the server
    self.identity = identity

    # find credential unless the protocol is http:
    if protocol == "http":
        self.certFile = None
        self.keyFile = None
    else:
        self.certFile, self.keyFile = findCredential()

  def ping(self):
    """
    """
    server = self.server
    protocol = self.protocol

    if protocol == "https":
    #if self.certFile and self.keyFile:
        h = six.moves.http_client.HTTPSConnection(server, key_file = self.keyFile, cert_file = self.certFile)
    else:
        h = six.moves.http_client.HTTPConnection(server)

    url = "/ldbd/ping.json"
    headers = {"Content-type" : "application/json"}
    data = ""
    body = encode(protocol)

    try:
        h.request("POST", url, body, headers)
        response = h.getresponse()
    except Exception as e:
        msg = "Error pinging server %s: %s" % (server, e)
        raise LDBDClientException(msg)

    if response.status != 200:
        msg = "Server returned code %d: %s" % (response.status, response.reason)
        body = response.read()
        msg += body
        raise LDBDClientException(msg)

    # since status is 200 OK the ping was good
    body = response.read()
    msg  = decode(body)

    return msg

  def query(self,sql):
    """
    """

    server = self.server
    protocol = self.protocol
    if protocol == "https":
        h = six.moves.http_client.HTTPSConnection(server, key_file = self.keyFile, cert_file = self.certFile)
    else:
        h = six.moves.http_client.HTTPConnection(server)

    url = "/ldbd/query.json"
    headers = {"Content-type" : "application/json"}
    body = encode(protocol + ":" + sql)

    try:
        h.request("POST", url, body, headers)
        response = h.getresponse()
    except Exception as e:
        msg = "Error querying server %s: %s" % (server, e)
        raise LDBDClientException(msg)

    if response.status != 200:
        msg = "Server returned code %d: %s" % (response.status, response.reason)
        body = response.read()
        msg += body
        raise LDBDClientException(msg)

    # since status is 200 OK the query was good
    body = response.read()
    msg  = decode(body)

    return msg

  def insert(self,xmltext):
    """
    """

    server = self.server
    protocol = self.protocol
    if protocol == "https":
        h = six.moves.http_client.HTTPSConnection(server, key_file = self.keyFile, cert_file = self.certFile)
    else:
        msg = "Insecure connection DOES NOT surpport INSERT."
        msg += '\nTo INSERT, authorized users please specify protocol "https" in your --segment-url argument.'
        msg += '\nFor example, "--segment-url https://segdb.ligo.caltech.edu".'
        raise LDBDClientException(msg)

    url = "/ldbd/insert.json"
    headers = {"Content-type" : "application/json"}
    body = encode(xmltext)

    try:
        h.request("POST", url, body, headers)
        response = h.getresponse()
    except Exception as e:
        msg = "Error querying server %s: %s" % (server, e)
        raise LDBDClientException(msg)

    if response.status != 200:
        msg = "Server returned code %d: %s" % (response.status, response.reason)
        body = response.read()
        msg += body
        raise LDBDClientException(msg)

    # since status is 200 OK the query was good
    body = response.read()
    msg  = decode(body)

    return msg

  def insertmap(self,xmltext,lfnpfn_dict):
    """
    """
    server = self.server
    protocol = self.protocol
    if protocol == "https":
        h = six.moves.http_client.HTTPSConnection(server, key_file = self.keyFile, cert_file = self.certFile)
    else:
        msg = "Insecure connection DOES NOT surpport INSERTMAP."
        msg += '\nTo INSERTMAP, authorized users please specify protocol "https" in your --segment-url argument.'
        msg += '\nFor example, "--segment-url https://segdb.ligo.caltech.edu".'
        raise LDBDClientException(msg)

    url = "/ldbd/insertmap.json"
    headers = {"Content-type" : "application/json"}

    pmsg = six.moves.cPickle.dumps(lfnpfn_dict)
    data = [xmltext, pmsg]
    body = encode(data)

    try:
        h.request("POST", url, body, headers)
        response = h.getresponse()
    except Exception as e:
        msg = "Error querying server %s: %s" % (server, e)
        raise LDBDClientException(msg)

    if response.status != 200:
        msg = "Server returned code %d: %s" % (response.status, response.reason)
        body = response.read()
        msg += body
        raise LDBDClientException(msg)

    # since status is 200 OK the query was good
    body = response.read()
    msg  = decode(body)

    return msg


  def insertdmt(self,xmltext):
    """
    """
    server = self.server
    protocol = self.protocol
    if protocol == "https":
        h = six.moves.http_client.HTTPSConnection(server, key_file = self.keyFile, cert_file = self.certFile)
    else:
        msg = "Insecure connection DOES NOT surpport INSERTDMT."
        msg += '\nTo INSERTDMT, authorized users please specify protocol "https" in your --segment-url argument.'
        msg += '\nFor example, "--segment-url https://segdb.ligo.caltech.edu".'
        raise LDBDClientException(msg)

    url = "/ldbd/insertdmt.json"
    headers = {"Content-type" : "application/json"}
    body = encode(xmltext)

    try:
        h.request("POST", url, body, headers)
        response = h.getresponse()
    except Exception as e:
        msg = "Error querying server %s: %s" % (server, e)
        raise LDBDClientException(msg)

    if response.status != 200:
        msg = "Server returned code %d: %s" % (response.status, response.reason)
        body = response.read()
        msg += body
        raise LDBDClientException(msg)

    # since status is 200 OK the query was good
    body = response.read()
    msg  = decode(body)

    return msg

