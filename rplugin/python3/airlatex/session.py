import pynvim
import browser_cookie3
import requests
import json
from bs4 import BeautifulSoup
import time
from threading import Thread, currentThread
from queue import Queue
import re
from airlatex.project_handler import AirLatexProject
from airlatex.util import _genTimeStamp
# from project_handler import AirLatexProject # FOR DEBUG MODE
# from util import _genTimeStamp # FOR DEBUG MODE

cj = browser_cookie3.load()


import traceback
def catchException(fn):
    def wrapped(self, nvim, *args, **kwargs):
        try:
            return fn(self, nvim, *args, **kwargs)
        except Exception as e:
            # nvim.err_write(traceback.format_exc(e)+"\n")
            nvim.err_write(str(e)+"\n")
            raise e
    return wrapped


### All web page related airlatex stuff
class AirLatexSession:
    def __init__(self, domain, servername, sidebar):
        self.sidebar = sidebar
        self.servername = servername
        self.domain = domain
        self.url = "https://"+domain
        self.authenticated = False
        self.httpHandler = requests.Session()
        self.cached_projectList = []
        self.projectThreads = []
        self.status = ""

    @catchException
    def cleanup(self, nvim):
        for p in self.cached_projectList:
            if "handler" in p:
                p["handler"]
        for t in self.projectThreads:
            t.stop()

    @catchException
    def login(self, nvim):
        if not self.authenticated:
            self.updateStatus(nvim, "Connecting")
            # check if cookie found by testing if projects redirects to login page
            redirect  = self.httpHandler.get(self.url + "/projects", cookies=cj)
            if len(redirect.history) == 0:
                self.authenticated = True
                self.updateProjectList(nvim)
                return True
            else:
                return False
        else:
            return False

    # Returns a list of airlatex projects
    @catchException
    def projectList(self, nvim):
        return self.cached_projectList

    @catchException
    def updateProjectList(self, nvim):
        if self.authenticated:

            def loading(self, nvim):
                i = 0
                t = currentThread()
                while getattr(t, "do_run", True):
                    s = " .." if i%3 == 0 else ". ." if i%3 == 1 else ".. "
                    self.updateStatus(nvim, s+" Loading "+s)
                    i += 1
                    time.sleep(0.1)
            thread = Thread(target=loading, args=(self,nvim), daemon=True)
            thread.start()

            projectPage = self.httpHandler.get(self.url + "/project")
            projectSoup = BeautifulSoup(projectPage.text, features='lxml')
            data = projectSoup.findAll("script",attrs={'id':'data'})
            thread.do_run = False
            if len(data) == 0:
                self.updateStatus(nvim, "Offline. Please Login.")
                return []
            data = json.loads(data[0].text)
            self.user_id = re.search("user_id\s*:\s*'([^']+)'",projectPage.text)[1]
            self.updateStatus(nvim, "Online")

            self.cached_projectList = data["projects"]
            self.cached_projectList.sort(key=lambda p: p["lastUpdated"], reverse=True)
            self.triggerRefresh(nvim)

    # Returns a list of airlatex projects
    @catchException
    def connectProject(self, nvim, project):
        if self.authenticated:

            # This is needed because IOLoop and pynvim interfere!
            msg_queue = Queue()
            msg_queue.put(("msg",None,"Connecting Project"))
            project["msg_queue"] = msg_queue
            def flush_queue(queue, project, servername):
                t = currentThread()
                nvim = pynvim.attach("socket",path=servername)
                while getattr(t, "do_run", True):
                    cmd, doc, data = queue.get()
                    try:
                        if cmd == "msg":
                            project["msg"] = data
                            nvim.command("call AirLatex_SidebarRefresh()")
                            time.sleep(0.1)
                            continue

                        buf = doc["buffer"]
                        if cmd == "applyUpdate":
                            buf.applyUpdate(data)
                        elif cmd == "write":
                            buf.write(data)
                        elif cmd == "updateRemoteCursor":
                            buf.updateRemoteCursor(data)
                    except Exception as e:
                        project["msg"] = "Exception:"+str(e)
                        nvim.command("call AirLatex_SidebarRefresh()")
            msg_thread = Thread(target=flush_queue, args=(msg_queue, project, self.servername), daemon=True)
            msg_thread.start()
            self.projectThreads.append(msg_thread)

            # start connection
            def initProject():
                nvim = pynvim.attach("socket",path=self.servername)
                # project["msg"] = "blub"
                # self.triggerRefresh(nvim)
                try:
                    AirLatexProject(self._getWebSocketURL(), project, self.user_id, msg_queue, msg_thread)
                except Exception as e:
                    # nvim.out_write(str(e)+"\n")
                    nvim.err_write(traceback.format_exc(e)+"\n")
            thread = Thread(target=initProject,daemon=True)
            self.projectThreads.append(thread)
            thread.start()

    @catchException
    def updateStatus(self, nvim, msg):
        self.status = msg
        nvim.command("call AirLatex_SidebarUpdateStatus()")

    @catchException
    def triggerRefresh(self, nvim):
        nvim.command("call AirLatex_SidebarRefresh()")

    def _getWebSocketURL(self):
        if self.authenticated:
            # Generating timestamp
            timestamp = _genTimeStamp()

            # To establish a websocket connection
            # the client must query for a sec url
            self.httpHandler.get(self.url + "/project")
            channelInfo = self.httpHandler.get(self.url + "/socket.io/1/?t="+timestamp)
            wsChannel = channelInfo.text[0:channelInfo.text.find(":")]
            return "wss://" + self.domain + "/socket.io/1/websocket/"+wsChannel



# for debugging
if __name__ == "__main__":
    import asyncio
    from mock import Mock
    import os
    DOMAIN = os.environ["DOMAIN"]
    sidebar = Mock()
    nvim = Mock()
    pynvim = Mock()
    async def main():
        sl = AirLatexSession(DOMAIN, None, sidebar)
        sl.login(nvim)
        project = sl.projectList(nvim)[1]
        print(">>>>",project)
        sl.connectProject(nvim, project)
        time.sleep(3)
        # print(">>>",project)
        doc = project["rootFolder"][0]["docs"][0]
        project["handler"].joinDocument(doc)
        time.sleep(6)
        print(">>>> sending ops")
        # project["handler"].sendOps(doc, [{'p': 0, 'i': '0abB\n'}])
        # project["handler"].sendOps(doc, [{'p': 0, 'i': 'def\n'}])
        # project["handler"].sendOps(doc, [{'p': 0, 'i': 'def\n'}])

    asyncio.run(main())