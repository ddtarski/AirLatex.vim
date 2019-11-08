import vim
from difflib import SequenceMatcher
from threading import Lock

if "allBuffers" not in globals():
    allBuffers = {}
class DocumentBuffer:
    allBuffers = allBuffers

    def __init__(self, path):
        self.path = path
        self.project_handler = path[0]["handler"]
        self.document = path[-1]
        self.initDocumentBuffer()
        self.buffer_mutex = Lock()
        self.saved_buffer = None

    def getName(self):
        return "/".join([p["name"] for p in self.path])
    def getExt(self):
        return self.document["name"].split(".")[-1]

    def initDocumentBuffer(self):

        # Creating new Buffer
        vim.command('wincmd w')
        vim.command('enew')
        vim.command('file '+self.getName())
        self.buffer = vim.current.buffer
        DocumentBuffer.allBuffers[self.buffer] = self

        # Buffer Settings
        vim.command("syntax on")
        vim.command('setlocal noswapfile')
        vim.command('setlocal buftype=nofile')
        vim.command("set filetype="+self.getExt())

        # self.applyString(serverBuffer)

        # ??? Returning normal function to these buttons
        # vim.command("nmap <silent> <up> <up>")
        # vim.command("nmap <silent> <down> <down>")
        # vim.command("nmap <silent> <enter> <enter>")
        # vim.command("set updatetime=500")
        # vim.command("autocmd CursorMoved,CursorMovedI * :call AirLatex_update_pos()")
        # vim.command("autocmd CursorHold,CursorHoldI * :call AirLatex_update_pos()")
        vim.command("au CursorMoved <buffer> call AirLatex_writeBuffer()")
        vim.command("au CursorMovedI <buffer> call AirLatex_writeBuffer()")
        vim.command("command! -buffer -nargs=0 W call AirLatex_writeBuffer()")

    def write(self, lines):
        def writeLines(buffer,lines):
            buffer[0] = lines[0]
            for l in lines[1:]:
                buffer.append(l)
            self.saved_buffer = buffer[:]
        # self.serverBuffer = "\n".join(lines)
        vim.async_call(writeLines,self.buffer,lines)

    def updateRemoteCursor(self, cursor):
        def updateRemoteCursor(cursor):
            vim.command("match ErrorMsg #\%"+str(cursor["row"])+"\%"+str(cursor["column"])+"v#")
            print(cursor)
        vim.async_call(updateRemoteCursor, cursor)

    def writeBuffer(self):

        # update CursorPosition
        self.project_handler.updateCursor(self.document,vim.current.window.cursor)

        # skip if not yet initialized
        if self.saved_buffer is None:
            return

        # nothing to do
        if len(self.saved_buffer) == len(self.buffer):
            skip = True
            for ol,nl in zip(self.saved_buffer, self.buffer):
                if hash(ol) != hash(nl):
                    skip = False
                    break
            if skip:
                return

        # calculate diff
        old = "\n".join(self.saved_buffer)
        new = "\n".join(self.buffer)
        S = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        ops = []
        for op in S:
            if op[0] == "equal":
                continue

            elif op[0] == "replace":
                ops.append({"p": op[1], "i": new[op[3]:op[4]]})
                ops.append({"p": op[1], "d": old[op[1]:op[2]]})

            elif op[0] == "insert":
                ops.append({"p": op[1], "i": new[op[3]:op[4]]})

            elif op[0] == "delete":
                ops.append({"p": op[1], "d": old[op[1]:op[2]]})

        # nothing to do
        if len(ops) == 0:
            return

        # reverse, as last op should be applied first
        ops.reverse()

        # update saved buffer & send command
        self.saved_buffer = self.buffer[:]
        self.project_handler.sendOps(self.document, ops)

    def applyUpdate(self,ops):

        # adapt version
        if "v" in ops:
            v = ops["v"]
            if v > self.document["version"]:
                self.document["version"] = v

        # do nothing if no op included
        if not 'op' in ops:
            return
        ops = ops['op']

        # async execution
        def applyOps(self, ops):
            self.buffer_mutex.acquire()
            try:
                for op in ops:

                    # delete char and lines
                    if 'd' in op:
                        p = op['p']
                        s = op['d']
                        self._remove(self.buffer,p,s)
                        self._remove(self.saved_buffer,p,s)

                    # add characters and newlines
                    if 'i' in op:
                        p = op['p']
                        s = op['i']
                        self._insert(self.buffer,p,s)
                        self._insert(self.saved_buffer,p,s)
            finally:
                self.buffer_mutex.release()
        vim.async_call(applyOps, self, ops)

    # inster string at given position
    def _insert(self, buffer, start, string):
        p_linestart = 0

        # find start line
        for line_i, line in enumerate(self.buffer):

            # start is not yet there
            if start >= p_linestart+len(line)+1:
                p_linestart += len(line)+1
            else:
                break

        # convert format to array-style
        string = string.split("\n")

        # append end of current line to last line of new line
        string[-1] += line[(start-p_linestart):]

        # include string at start position
        buffer[line_i] = line[:(start-p_linestart)] + string[0]

        # append rest to next line
        if len(string) > 1:
            buffer[line_i+1:line_i+1] = string[1:]

    # remove len chars from pos
    def _remove(self, buffer, start, string):
        p_linestart = 0

        # find start line
        for line_i, line in enumerate(buffer):

            # start is not yet there
            if start >= p_linestart+len(line)+1:
                p_linestart += len(line)+1
            else:
                break

        # convert format to array-style
        string = string.split("\n")
        new_string = ""

        # remove first line from found position
        new_string = line[:(start-p_linestart)]

        # add rest of last line to new string
        if len(string) == 1:
            new_string += buffer[line_i+len(string)-1][(start-p_linestart)+len(string[-1]):]
        else:
            new_string += buffer[line_i+len(string)-1][len(string[-1]):]

        # overwrite buffer
        buffer[line_i:line_i+len(string)] = [new_string]


