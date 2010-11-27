import tornado.escape
class Widgets(object):
    def tag(self,tag):
        return '<a href="#" class="tag">%(u)s</a>' % {'u':tornado.escape.xhtml_escape(tag[:32])}
    def club(self,club):
        return '<a href="#" class="club">%(u)s</a>' % {'u':tornado.escape.xhtml_escape(club[:32])}
    def tags(self,tags,clubs):
        return '<div class="tags"> '+' '.join(self.club(c) for c in clubs)+' '+' '.join(self.tag(t) for t in tags)+' </div>'
    def user_url(self,name):
        return '/u/%(u)s' % {'u':name}
    def userl(self,name):
        return '<a href="/u/%(u)s" class="usrid">@%(u)s</a>' % {'u':name}
    def msgl(self,msg):
        return '<a href="/p/%(u)s" class="msgid">#%(n)s</a>' % {'u':msg.replace('/','#'),'n':msg}
widgets=Widgets()