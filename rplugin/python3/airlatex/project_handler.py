import pynvim
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado import gen
from tornado.websocket import websocket_connect
import re
from itertools import count
import json
from airlatex.util import _genTimeStamp, getLogger
import time
from tornado.locks import Lock, Event
from logging import DEBUG
from tornado.httpclient import HTTPRequest
from asyncio import Queue

codere = re.compile(r"(\d):(?:(\d+)(\+?))?:(?::(?:(\d+)(\+?))?(.*))?")
# code, await_id, await_mult, answer_id, answer_mult, msg = codere.match(str).groups()
# code        : m[0]
# await_id    : m[1]
# await_mult  : m[2]
# answer_id   : m[3]
# answer_mult : m[5]
# msg         : m[5]

class AirLatexProject:

    def __init__(self, url, project, used_id, sidebar, cookie=None):
        project["handler"] = self

        self.sidebar = sidebar
        self.ioloop = IOLoop()
        self.used_id = used_id
        self.project = project
        self.url = url
        self.cookie = cookie
        self.url_base = url.split("/")[2]
        self.command_counter = count(1)
        self.ws = None
        self.requests = {}
        self.cursors = {}
        self.documents = {}
        self.log = getLogger(__name__)
        self.ops_queue = Queue()
        self.log.debug("init done")

    async def start(self):
        self.log.debug("start")
        # start tornado event loop & related callbacks
        IOLoop.current().spawn_callback(self.sendOps_flush)
        PeriodicCallback(self.keep_alive, 20000).start()
        await self.connect()
        await self.ioloop.start()

    async def send(self,message_type,message=None,event=None):
        if message_type == "keep_alive":
            self.log.debug("send keep_alive")
            self.ws.write_message("2::")
            return
        assert message is not None
        message_content = json.dumps(message) if isinstance(message, dict) else message
        message["event"] = event
        if message_type == "update":
            self.log.debug("send update: "+message_content)
            self.ws.write_message("5:::"+message_content)
        elif message_type == "cmd":
            cmd_id = next(self.command_counter)
            msg = "5:" + str(cmd_id) + "+::" + message_content
            self.log.debug("send cmd: "+msg)
            self.requests[str(cmd_id)] = message
            self.ws.write_message(msg)

    async def sidebarMsg(self, msg):
        self.log.debug("sidebarMsg: %s" % msg)
        self.project["msg"] = msg
        await self.sidebar.triggerRefresh()

    async def gui_await(self, waiting=True):
        self.project["await"] = waiting
        await self.sidebar.triggerRefresh()

    async def bufferDo(self, doc_id, command, data):
        if doc_id in self.documents:
            doc = self.documents[doc_id]
            buf = doc["buffer"]
            self.log.debug("cmd="+command)
            if command == "applyUpdate":
                buf.applyUpdate(data)
            elif command == "write":
                buf.write(data)
            elif command == "updateRemoteCursor":
                buf.updateRemoteCursor(data)

    async def updateRemoteCursor(self, cursors):
        for cursor in cursors:
            if "row" in cursor and "column" in cursor and "doc_id" in cursor:
                await self.bufferDo(cursor["doc_id"], "updateRemoteCursor", cursor)

    async def triggerSidebarRefresh(self):
        self.log.debug("triggerSidebarRefresh()")
        await self.sidebar.triggerRefresh()

    async def updateCursor(self,doc, pos):
        event = Event()
        await self.send("update",{
            "name":"clientTracking.updatePosition",
            "args": [{
                "doc_id": doc["_id"],
                "row": pos[0]-1,
                "column": pos[1]
            }]
        }, event=event)

    # wrapper for the ioloop
    async def sendOps(self, document, ops=[]):
        await self.ops_queue.put((document, ops))

    # actual sending of ops
    async def _sendOps(self, document, ops=[]):
        self.log.debug("_sendOps(doc=%s, ops=%s)" % (document["_id"], str(len(ops))))

        # append new ops to buffer
        document["ops_buffer"] += ops

        # skip if nothing to do
        if len(document["ops_buffer"]) == 0:
            return

        # wait if awaiting server response
        event = Event()
        await self.gui_await(True)
        self.log.debug("ops await accept -> new request")

        # clean buffer for next call
        ops_buffer, document["ops_buffer"] = document["ops_buffer"], []

        # actually send operations
        source = document["_id"]

        obj_to_send = {
            "doc": document["_id"],
            # "meta": {
            #     "source": source,
            #     "ts": _genTimeStamp(),
            #     "user_id": self.used_id
            # },
            "op": ops_buffer,
            "v": document["version"],
            "lastV": document["version"]-1
        }
        if document["buffer"].content_hash:
            obj_to_send["hash"] = document["buffer"].content_hash # overleaf/web: sends document hash (if it hasn't been sent in the last 5 seconds)

        await self.send("cmd",{
            "name":"applyOtUpdate",
            "args": [
                document["_id"],
                obj_to_send
            ]
        }, event=event)
        self.log.debug("ops await accept -> wait")
        await event.wait()
        await self.gui_await(False)
        self.log.debug("ops await accept -> done")

    # sendOps whenever events appear in queue
    # (is only called in constructor)
    async def sendOps_flush(self):
        self.log.debug("starting sendOps_flush()")

        # direct sending
        # async for document, ops in self.ops_queue:
        #     self.log.debug("sendOps_flush() -> _sendOps")
        #     await self._sendOps(document, ops)
        #     self.log.debug("sendOps_flush() -> _sendOps done")

        # collects ops and sends them in a batch, server is ready
        while True:
            self.log.debug("sendOps_flush() -> waiting")
            all_ops = {}

            # await first element
            document, ops = await self.ops_queue.get()
            self.log.debug("sendOps_flush() -> got first")
            if document["_id"] not in all_ops:
                all_ops[document["_id"]] = ops
            else:
                all_ops[document["_id"]] += ops

            # get also all other elements that are currently in queue
            num = self.ops_queue.qsize()
            for i in range(num):
                self.log.debug("sendOps_flush() -> got another "+str(num))
                document, ops = await self.ops_queue.get()
                self.log.debug("sendOps_flush() -> got another")
                if document["_id"] not in all_ops:
                    all_ops[document["_id"]] = ops
                else:
                    all_ops[document["_id"]] += ops
                self.log.debug("sendOps_flush() -> got another end")

            # apply all ops one after another
            self.log.debug("sendOps_flush() -> sending")
            for doc_id, ops in all_ops.items():
                document = self.documents[doc_id]
                await self._sendOps(document, ops)




    async def joinDocument(self, buffer):

        # register buffer in document
        doc = buffer.document
        doc["buffer"] = buffer

        # register document in project_handler
        self.documents[doc["_id"]] = doc

        # register document op-buffer
        self.documents[doc["_id"]]["ops_buffer"] = []

        # regester for document-watching
        await self.send("cmd",{
            "name":"joinDoc",
            "args": [
                doc["_id"],
                {"encodeRanges": True}
            ]
        })

    async def disconnect(self):
        del self.project["handler"]
        # self.msg_thread.do_run = False
        self.log.debug("Connection Closed")
        await self.ioloop.stop()
        await self.sidebarMsg("Disconnected.")
        self.project["open"] = False
        self.project["connected"] = False
        await self.triggerSidebarRefresh()

    async def connect(self):
        try:
            await self.sidebarMsg("Connecting Websocket.")
            self.project["connected"] = True
            self.log.debug("Websocket Connecting to "+self.url)
            request = HTTPRequest(self.url, headers={'Cookie': self.cookie})
            self.ws = await websocket_connect(request)
        except Exception as e:
            await self.sidebarMsg("Connection Error: "+str(e))
        else:
            await self.sidebarMsg("Connected.")
            await self.run()

    async def run(self):
        try:
            while True:
                msg = await self.ws.read_message()
                # if msg is None:
                #     await self.sidebarMsg("Connection Closed")
                #     self.ws = None
                #     break
                self.log.debug("answer: "+msg)

                # parse the code
                code, await_id, await_mult, answer_id, answer_mult, data = codere.match(msg).groups()
                if data:
                    try:
                        data = json.loads(data)
                    except:
                        data = {"name":"error"}

                # error occured
                if code == "0":
                    await self.sidebarMsg("The server closed the connection.")
                    self.disconnect()

                # first message
                elif code == "1":
                    pass

                # keep alive
                elif code == "2":
                    self.keep_alive()

                # server request
                elif code == "5":
                    if not isinstance(data,dict):
                        pass

                    # connection accepted => join Project
                    if data["name"] == "connectionAccepted":
                        await self.sidebarMsg("Connection Active.")
                        await self.send("cmd",{"name":"joinProject","args":[{"project_id":self.project["id"]}]})

                    # broadcastDocMeta => we ignore it at first
                    elif data["name"] == "broadcastDocMeta":
                        pass

                    # client Connected => delete from cursor list
                    elif data["name"] == "clientTracking.clientUpdated":
                        for cursor in data["args"]:
                            self.cursors[cursor["id"]].update(cursor)
                        await self.updateRemoteCursor(data["args"])

                    # client Disconnected => delete from cursor list
                    elif data["name"] == "clientTracking.clientDisconnected":
                        for id in data["args"]:
                            if id in self.cursors:
                                del self.cursors[id]
                        await self.updateRemoteCursor(data["args"])

                    # update applied => apply update to buffer
                    elif data["name"] == "otUpdateApplied":

                        # nothing to do?
                        if "args" not in data:
                            return

                        # apply update to buffer
                        for op in data["args"]:
                            await self.bufferDo(op["doc"], "applyUpdate", op)

                    # error occured
                    elif data["name"] == "otUpdateError":
                        await self.sidebarMsg("Error occured on operation Update: " + data["args"][0])
                        await self.disconnect()

                    # unknown message
                    else:
                        await self.sidebarMsg("Data not known: "+msg)

                # answer to our request
                elif code == "6":

                    # get request command
                    request = self.requests[answer_id]
                    cmd = request["name"]

                    # joinProject => server lists project information
                    if cmd == "joinProject":
                        project_info = data[1]
                        if self.log.level == DEBUG:
                            self.log.debug(json.dumps(project_info))
                        self.project.update(project_info)
                        self.project["open"] = True
                        await self.send("cmd",{"name":"clientTracking.getConnectedUsers"})
                        await self.triggerSidebarRefresh()

                    elif cmd == "joinDoc":
                        id = request["args"][0]
                        self.documents[id]["version"] = data[2]
                        await self.bufferDo(id, "write", [d.encode("latin1").decode("utf8") for d in data[1]])

                    elif cmd == "applyOtUpdate":
                        id = request["args"][0]

                        # version increase should be before next event
                        self.documents[id]["version"] += 1

                        # flush next
                        request["event"].set()

                        # remove awaiting request
                        del self.requests[answer_id]

                    elif cmd == "clientTracking.getConnectedUsers":
                        for cursor in data[1]:
                            if "cursorData" in cursor:
                                cursorData = cursor["cursorData"]
                                del cursor["cursorData"]
                                cursor.update(cursorData)
                            self.cursors[cursor["client_id"]] = cursor
                        await self.updateRemoteCursor(data[1])

                    elif cmd == "clientTracking.updatePosition":
                        # server accepted the change
                        del self.requests[answer_id]

                    else:
                        await self.sidebarMsg("Data not known:"+str(msg))

                # answer to our request
                elif code == "7":
                    await self.sidebarMsg("Error: Unauthorized. My guess is that your session cookies are outdated or not loaded. Typically reloading '%s/project' using the browser you used for login should reload the cookies." % self.url_base)

                # unknown message
                else:
                    await self.sidebarMsg("Unknown Code:"+str(msg))
        except (gen.Return, StopIteration):
            raise
        except Exception as e:
            await self.sidebarMsg("Error: "+type(e)+" "+str(e))
            raise

    async def keep_alive(self):
        await self.send("keep_alive")

