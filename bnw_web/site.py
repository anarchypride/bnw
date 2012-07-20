# -*- coding: utf-8 -*-
from twisted.internet import epollreactor
#epollreactor.install()
from twisted.internet import reactor
from twisted.internet import interfaces, defer
from twisted.web.server import Site, NOT_DONE_YET
from twisted.web.resource import Resource, NoResource

import tornado.options
import tornado.httpserver
import tornado.web
import tornado.websocket
import tornado.escape
import logging,traceback
import json
import txmongo
import os
from widgets import widgets
from linkify import thumbify
import uimodules
import rss
import base64
import math

from tornado.options import define, options

import bnw_core.bnw_objects as objs
import bnw_core.post as post
import bnw_core.base
from bnw_core.bnw_mongo import get_db,get_fs
from bnw_handlers.command_show import cmd_feed,cmd_today
from bnw_handlers.command_clubs import cmd_clubs,cmd_tags

from base import BnwWebHandler, TwistedHandler, BnwWebRequest
from auth import LoginHandler, requires_auth, AuthMixin
from api import ApiHandler
define("port", default=8888, help="run on the given port", type=int)


class MessageHandler(BnwWebHandler,AuthMixin):
    templatename='message.html'
    @defer.inlineCallbacks
    def respond(self,msgid):
        user = yield self.get_auth_user()
        f = txmongo.filter.sort(txmongo.filter.ASCENDING("date"))
        msg=(yield objs.Message.find_one({'id': msgid}))
        comments=list((yield objs.Comment.find({'message': msgid},filter=f))) # ненавидь себя, сука
        self.set_header("Cache-Control", "max-age=5")
        if not msg:
            self.set_status(404)
        defer.returnValue({
            'msgid': msgid,
            'msg': msg,
            'auth_user': user,
            'comments': comments,
        })


class WsHandler(tornado.websocket.WebSocketHandler):
    """Helper class for websocket handlers.
    Register listeners and send new events to clients.
    Unregister listeners on close.
    """

    def get_handlers(self, *args):
        return ()

    def open(self, *args):
        self.version = self.request.arguments.get('v',['0'])[0]
        self.handlers = self.get_handlers(*args)
        for etype, handler in self.handlers:
            post.register_listener(etype, id(self), handler)
        print 'Opened connection %d (v%s)' % (id(self),self.version)

    def on_close(self):
        for etype, _ in self.handlers:
            post.unregister_listener(etype, id(self))
        print 'Closed connection %d' % id(self)


class MainWsHandler(WsHandler):
    """Deliver new events on main page via websockets."""

    def get_handlers(self):
        if self.version=='2':
            return (
                ('new_message', self.new_message),
                ('del_message', self.del_message),
                ('upd_comments_count', self.upd_comments_count),
                ('upd_recommendations_count', self.upd_recommendations_count),
            )
        else:
            return (
                ('new_message', self.new_message_compat),
            )

    def new_message(self, msg):
        html = uimodules.Message(self).render(msg)
        self.write_message(json.dumps({'type': 'new_message', 'html': html}))

    def new_message_compat(self, msg):
        self.write_message(json.dumps(msg))

    def del_message(self, msg_id):
        self.write_message(json.dumps({'type': 'del_message', 'id': msg_id}))

    def upd_comments_count(self, msg_id, num):
        self.write_message(json.dumps({
            'type': 'upd_comments_count', 'id': msg_id, 'num': num}))

    def upd_recommendations_count(self, msg_id, num):
        self.write_message(json.dumps({
            'type': 'upd_recommendations_count', 'id': msg_id, 'num': num}))


class MessageWsHandler(WsHandler):
    """Deliver new events on message page via websockets."""

    def get_handlers(self, msgid):
        if self.version=='2':
            return (
                ('new_comment_in_'+msgid, self.new_comment),
                ('del_comment_in_'+msgid, self.del_comment),
            )
        else:
            return (
                ('new_comment_in_'+msgid, self.new_comment_compat),
            )

    def new_comment(self, comment):
        html = uimodules.Comment(self).render(comment)
        self.write_message(json.dumps({'type': 'new_comment', 'html': html}))

    def new_comment_compat(self, comment):
        self.write_message(json.dumps(comment))

    def del_comment(self, comment_id):
        self.write_message(json.dumps(
            {'type': 'del_comment', 'id': comment_id}))


def get_page(self):
    ra = self.request.arguments
    rv = ra.get('page',['0'])[0]
    if rv.isdigit():
        rv=int(rv)
        return rv if isinstance(rv,int) else 0 # no long here
    return 0


@defer.inlineCallbacks
def is_hasmes(qdict, page):
    """Return True if summary page count bigger than given
    (given page numbering starting from 0).
    """
    count = yield objs.Message.count(qdict)
    defer.returnValue(int(math.ceil(count/20.0)) > page+1)


class UserHandler(BnwWebHandler,AuthMixin):
    templatename='user.html'
    @defer.inlineCallbacks
    def respond(self,username,tag=None):
        _ = yield self.get_auth_user()
        f = txmongo.filter.sort(txmongo.filter.DESCENDING("date"))
        user = (yield objs.User.find_one({'name': username}))
        page = get_page(self)
        qdict = { 'user': username }
        if tag:
            tag = tornado.escape.url_unescape(tag)
            qdict['tags'] = tag
        messages=(yield objs.Message.find(qdict,filter=f,limit=20,skip=20*page))
        hasmes = yield is_hasmes(qdict, page)

        format=self.get_argument("format","")
        if format=='rss':
            self.set_header("Content-Type", 'application/rss+xml; charset=UTF-8')
            defer.returnValue(rss.message_feed(messages,
                        link=widgets.user_url(username),
                        title='Поток сознания @%s' % username))
        elif format=='json':
            json_messages=[message.filter_fields() for message in messages]
            defer.returnValue(json.dumps(json_messages,ensure_ascii=False))
        else:
            self.set_header("Cache-Control", "max-age=1")
            defer.returnValue({
                'username': username,
                'user': user,
                'messages': messages,
                'page': page,
                'tag' : tag,
                'hasmes': hasmes,
            })

class UserRecoHandler(BnwWebHandler,AuthMixin):
    templatename='user.html'
    @defer.inlineCallbacks
    def respond(self,username,tag=None):
        _ = yield self.get_auth_user()
        f = txmongo.filter.sort(txmongo.filter.DESCENDING("date"))
        user = (yield objs.User.find_one({'name': username}))
        page = get_page(self)
        qdict = { 'recommendations': username }
        if tag:
            tag = tornado.escape.url_unescape(tag)
            qdict['tags'] = tag
        messages=(yield objs.Message.find(qdict,filter=f,limit=20,skip=20*page))
        hasmes = yield is_hasmes(qdict, page)

        self.set_header("Cache-Control", "max-age=1")
        defer.returnValue({
                'username': username,
                'user': user,
                'messages': messages,
                'page': page,
                'tag' : tag,
                'hasmes': hasmes,
            })


class UserInfoHandler(BnwWebHandler,AuthMixin):
    templatename='userinfo.html'
    @defer.inlineCallbacks
    def respond(self,username):
        _ = yield self.get_auth_user()
        user = yield objs.User.find_one({'name': username})
        subscribers = set([x['user'] for x in
                    (yield objs.Subscription.find({'target':username,'type':'sub_user'}))])
        subscriptions = set([x['target'] for x in
                    (yield objs.Subscription.find({'user':username,'type':'sub_user'}))])
        friends = list(subscribers & subscriptions)
        friends.sort()
        subscribers_only = list(subscribers - subscriptions)
        subscribers_only.sort()
        subscriptions_only = list(subscriptions - subscribers)
        subscriptions_only.sort()
        messages_count = int((yield objs.Message.count({'user': username})))
        comments_count = int((yield objs.Comment.count({'user': username})))
        self.set_header("Cache-Control", "max-age=10")
        defer.returnValue({
            'username': username,
            'user': user,
            'regdate': user.get('regdate', 0),
            'messages_count': messages_count,
            'comments_count': comments_count,
            'subscribers': subscribers_only,
            'subscriptions': subscriptions_only,
            'friends': friends,
            'vcard': user.get('vcard', {}),
        })


class MainHandler(BnwWebHandler,AuthMixin):
    templatename='main.html'
    @defer.inlineCallbacks
    def respond(self,club=None,tag=None):
        f = txmongo.filter.sort(txmongo.filter.DESCENDING("date"))

        user=yield self.get_auth_user()

        page = get_page(self)
        qdict = {}
        if tag:
            tag = tornado.escape.url_unescape(tag)
            qdict['tags'] = tag
        if club:
            club = tornado.escape.url_unescape(club)
            qdict['clubs'] = club

        messages=(yield objs.Message.find(qdict,filter=f,limit=20,skip=20*page))
        hasmes = yield is_hasmes(qdict, page)
        uc=(yield objs.User.count())
        format=self.get_argument("format","")

        self.set_header("Cache-Control", "max-age=1")
        if format=='rss':
            self.set_header("Content-Type", 'application/rss+xml; charset=UTF-8')
            if self.request.protocol == "https":
                base = bnw_core.base.get_https_webui_base()
            else:
                base = bnw_core.base.get_http_webui_base()
            defer.returnValue(
                rss.message_feed(
                    messages,link=base,
                    title='Коллективное бессознательное BnW'))

        elif format=='json':
            json_messages=[message.filter_fields() for message in messages]
            defer.returnValue(json.dumps(json_messages,ensure_ascii=False))

        else:
            req=BnwWebRequest((yield self.get_auth_user()))
            tagres = yield cmd_tags(req)
            toptags = tagres['tags'] if tagres['ok'] else []
            defer.returnValue({
                'messages': messages,
                'toptags': toptags,
                'users_count':int(uc),
                'page': page,
                'tag': tag,
                'club': club,
                'hasmes': hasmes,
            })

class FeedHandler(BnwWebHandler,AuthMixin):
    templatename='feed.html'
    @requires_auth
    @defer.inlineCallbacks
    def respond(self,page=0):
        req=BnwWebRequest((yield self.get_auth_user()))
        result = yield cmd_feed(req)
        self.set_header("Cache-Control", "max-age=1")
        defer.returnValue({
            'result': result,
        })

class TodayHandler(BnwWebHandler,AuthMixin):
    templatename='today.html'
    @defer.inlineCallbacks
    def respond(self,page=0):
        req=BnwWebRequest((yield self.get_auth_user()))
        result = yield cmd_today(req)
        self.set_header("Cache-Control", "max-age=300")
        defer.returnValue({
            'result': result,
        })

class ClubsHandler(BnwWebHandler,AuthMixin):
    templatename='clubs.html'
    @defer.inlineCallbacks
    def respond(self,page=0):
        user=yield self.get_auth_user()
        req=BnwWebRequest((yield self.get_auth_user()))
        result = yield cmd_clubs(req)
        self.set_header("Cache-Control", "max-age=3600")
        defer.returnValue({
            'result': result,
        })

class BlogHandler(BnwWebHandler,AuthMixin):
    @requires_auth
    @defer.inlineCallbacks
    def respond(self,page=0):
        user=yield self.get_auth_user()
        self.redirect(str('/u/'+user['name']))
        defer.returnValue('')

class PostHandler(BnwWebHandler,AuthMixin):
    templatename='post.html'
    @requires_auth
    @defer.inlineCallbacks
    def respond_post(self):
        tags=[i[:128] for i in self.get_argument("tags","").split(",",5)[:5] if i]
        clubs=[i[:128] for i in self.get_argument("clubs","").split(",",5)[:5] if i]
        text=self.get_argument("text","")
        user = yield self.get_auth_user()
        ok,result = yield post.postMessage(user,tags,clubs,text)
        if ok:
            (msg_id,qn,recs) = result
            self.redirect('/p/'+msg_id)
            defer.returnValue('')
        else:
            defer.returnValue({'error':result})
    @requires_auth
    @defer.inlineCallbacks
    def respond(self):
        user = yield self.get_auth_user()
        default_text = self.get_argument("url","")
        self.set_header("Cache-Control", "max-age=1")
        defer.returnValue({ 'auth_user': user, 'default_text': default_text, 'error':None })


class CommentHandler(BnwWebHandler,AuthMixin):
    templatename='comment.html'
    @requires_auth
    @defer.inlineCallbacks
    def respond_post(self):
        msg=self.get_argument("msg","")
        comment=self.get_argument("comment","")
        if comment:
            comment=msg+"/"+comment
        text=self.get_argument("text","")
        noredir=self.get_argument("noredir","")
        user = yield self.get_auth_user()
        ok,result = yield post.postComment(msg,comment,text,user)
        if ok:
            (msg_id,num,qn,recs) = result
            if noredir:
                defer.returnValue('Posted with '+msg_id)
            else:
                redirtarget='/p/'+msg_id.replace('/','#')
                # странная хуйня с твистедом или еще чем-то
                # если в редиректе unicode-объект - реквест не финиширует
                self.redirect(str(redirtarget))
                defer.returnValue('')
        else:
            defer.returnValue({'error':result})

emptypng = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABAQAAAAA3bvkkAAAAAnRSTlMAAHaTzTgAAAAKSURB'
    'VAjXY2AAAAACAAHiIbwzAAAAAElFTkSuQmCC')

class AvatarHandler(BnwWebHandler):
    @defer.inlineCallbacks
    def respond(self, username, thumb=''):
        self.set_header('Cache-Control', 'max-age=36000, public')
        self.set_header('Vary', 'Accept-Encoding')
        user = yield objs.User.find_one({'name': username})
        if not (user and user.get('avatar')):
            self.set_header('Content-Type', 'image/png')
            defer.returnValue(emptypng)
        if thumb:
            av_id = user['avatar'][2]
            mimetype = 'image/png'
        else:
            av_id = user['avatar'][0]
            mimetype = user['avatar'][1]
        fs = yield get_fs('avatars')
        # воркэраунд недопила в txmongo. TODO: зарепортить или починить
        doc = yield fs._GridFS__files.find_one({'_id': av_id})
        avatar = yield fs.get(doc)
        avatar_data = yield avatar.read()
        self.set_header('Content-Type', mimetype)
        defer.returnValue(avatar_data)

def get_site():
    settings={
        "template_path":os.path.join(os.path.dirname(__file__), "templates"),
        "xsrf_cookies": True,
        "static_path":  os.path.join(os.path.dirname(__file__), "static"),
        "ui_modules": uimodules,
        "autoescape": None,
    }
    application = tornado.web.Application([
        (r"/p/([A-Z0-9]+)/?", MessageHandler),
        (r"/p/([A-Z0-9]+)/ws/?", MessageWsHandler),
        (r"/u/([0-9a-z_-]+)/?", UserHandler),
        (r"/u/([0-9a-z_-]+)/recommendations/?", UserRecoHandler),
        (r"/u/([0-9a-z_-]+)/avatar(/thumb)?/?", AvatarHandler),
        (r"/u/([0-9a-z_-]+)/info/?", UserInfoHandler),
        (r"/u/([0-9a-z_-]+)/t/(.*)/?", UserHandler),
        (r"/", MainHandler),
        (r"/ws/?", MainWsHandler),
        (r"/t/()(.*)/?", MainHandler),
        (r"/c/(.*)()/?", MainHandler),
        (r"/login", LoginHandler),
        (r"/post", PostHandler),
        (r"/feed", FeedHandler),
        (r"/today", TodayHandler),
        (r"/clubs", ClubsHandler),
        (r"/blog", BlogHandler),
        (r"/comment", CommentHandler),
        (r"/api/([0-9a-z/]*)/?", ApiHandler),
    ],**settings)

    return tornado.httpserver.HTTPServer(application,xheaders=True)

def main():
    tornado.options.parse_command_line()
    site = get_site()
    reactor.listenTCP(options.port, site)

    reactor.run()

if __name__ == "__main__":
    main()
