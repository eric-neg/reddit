# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is Reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of the
# Original Code is CondeNet, Inc.
#
# All portions of the code written by CondeNet are Copyright (c) 2006-2010
# CondeNet, Inc. All Rights Reserved.
################################################################################
from validator import *
from pylons.i18n import _, ungettext
from reddit_base import RedditController, base_listing
from r2 import config
from r2.models import *
from r2.lib.pages import *
from r2.lib.pages.things import wrap_links
from r2.lib.jsontemplates import is_api
from r2.lib.menus import *
from r2.lib.utils import to36, sanitize_url, check_cheating, title_to_url
from r2.lib.utils import query_string, UrlParser, link_from_url, link_duplicates
from r2.lib.utils import randstr
from r2.lib.template_helpers import get_domain
from r2.lib.filters import unsafe
from r2.lib.emailer import has_opted_out, Email
from r2.lib.db.operators import desc
from r2.lib.db import queries
from r2.lib.strings import strings
from r2.lib.solrsearch import RelatedSearchQuery, SubredditSearchQuery
from r2.lib.indextank import IndextankQuery, IndextankException, InvalidIndextankQuery
from r2.lib.contrib.pysolr import SolrError
from r2.lib import jsontemplates
from r2.lib import sup
import r2.lib.db.thing as thing
from listingcontroller import ListingController
from pylons import c, request, request, Response

import string
import random as rand
import re, socket
import time as time_module
from urllib import quote_plus

class FrontController(RedditController):

    allow_stylesheets = True

    @validate(article = VLink('article'),
              comment = VCommentID('comment'))
    def GET_oldinfo(self, article, type, dest, rest=None, comment=''):
        """Legacy: supporting permalink pages from '06,
           and non-search-engine-friendly links"""
        if not (dest in ('comments','related','details')):
                dest = 'comments'
        if type == 'ancient':
            #this could go in config, but it should never change
            max_link_id = 10000000
            new_id = max_link_id - int(article._id)
            return self.redirect('/info/' + to36(new_id) + '/' + rest)
        if type == 'old':
            new_url = "/%s/%s/%s" % \
                      (dest, article._id36, 
                       quote_plus(title_to_url(article.title).encode('utf-8')))
            if not c.default_sr:
                new_url = "/r/%s%s" % (c.site.name, new_url)
            if comment:
                new_url = new_url + "/%s" % comment._id36
            if c.extension:
                new_url = new_url + "/.%s" % c.extension

            new_url = new_url + query_string(request.get)

            # redirect should be smarter and handle extensions, etc.
            return self.redirect(new_url, code=301)

    def GET_random(self):
        """The Serendipity button"""
        sort = rand.choice(('new','hot'))
        links = c.site.get_links(sort, 'all')
        if isinstance(links, thing.Query):
            links._limit = g.num_serendipity
            links = [x._fullname for x in links]
        else:
            links = list(links)[:g.num_serendipity]

        rand.shuffle(links)

        builder = IDBuilder(links, skip = True,
                            keep_fn = lambda x: x.fresh,
                            num = 1)
        links = builder.get_items()[0]

        if links:
            l = links[0]
            return self.redirect(add_sr("/tb/" + l._id36))
        else:
            return self.redirect(add_sr('/'))

    @validate(VAdmin(),
              article = VLink('article'))
    def GET_details(self, article):
        """The (now depricated) details page.  Content on this page
        has been subsubmed by the presence of the LinkInfoBar on the
        rightbox, so it is only useful for Admin-only wizardry."""
        return DetailsPage(link = article, expand_children=False).render()


    def GET_selfserviceoatmeal(self):
        return BoringPage(_("self service help"), 
                          show_sidebar = False,
                          content = SelfServiceOatmeal()).render()


    @validate(article = VLink('article'))
    def GET_shirt(self, article):
        if not can_view_link_comments(article):
            abort(403, 'forbidden')
        if g.spreadshirt_url:
            from r2.lib.spreadshirt import ShirtPage
            return ShirtPage(link = article).render()
        return self.abort404()

    def _comment_visits(self, article, user, new_visit=None):
        hc_key = "comment_visits-%s-%s" % (user.name, article._id36)
        old_visits = g.hardcache.get(hc_key, [])

        append = False

        if new_visit is None:
            pass
        elif len(old_visits) == 0:
            append = True
        else:
            last_visit = max(old_visits)
            time_since_last = new_visit - last_visit
            if (time_since_last.days > 0
                or time_since_last.seconds > g.comment_visits_period):
                append = True
            else:
                # They were just here a few seconds ago; consider that
                # the same "visit" as right now
                old_visits.pop()

        if append:
            copy = list(old_visits) # make a copy
            copy.append(new_visit)
            if len(copy) > 10:
                copy.pop(0)
            g.hardcache.set(hc_key, copy, 86400 * 2)

        return old_visits


    @validate(article      = VLink('article'),
              comment      = VCommentID('comment'),
              context      = VInt('context', min = 0, max = 8),
              sort         = VMenu('controller', CommentSortMenu),
              limit        = VInt('limit'),
              depth        = VInt('depth'))
    def POST_comments(self, article, comment, context, sort, limit, depth):
        # VMenu validator will save the value of sort before we reach this
        # point. Now just redirect to GET mode.
        return self.redirect(request.fullpath + query_string(dict(sort=sort)))

    @validate(article      = VLink('article'),
              comment      = VCommentID('comment'),
              context      = VInt('context', min = 0, max = 8),
              sort         = VMenu('controller', CommentSortMenu),
              limit        = VInt('limit'),
              depth        = VInt('depth'))
    def GET_comments(self, article, comment, context, sort, limit, depth):
        """Comment page for a given 'article'."""
        if comment and comment.link_id != article._id:
            return self.abort404()

        sr = Subreddit._byID(article.sr_id, True)

        if sr.name == g.takedown_sr:
            request.environ['REDDIT_TAKEDOWN'] = article._fullname
            return self.abort404()

        if not c.default_sr and c.site._id != sr._id:
            return self.abort404()

        if not can_view_link_comments(article):
            abort(403, 'forbidden')

        #check for 304
        self.check_modified(article, 'comments')

        # If there is a focal comment, communicate down to
        # comment_skeleton.html who that will be. Also, skip
        # comment_visits check
        previous_visits = None
        if comment:
            c.focal_comment = comment._id36
        elif (c.user_is_loggedin and c.user.gold and
              c.user.pref_highlight_new_comments):
            #TODO: remove this profiling if load seems okay
            from datetime import datetime
            before = datetime.now(g.tz)
            previous_visits = self._comment_visits(article, c.user, c.start_time)
            after = datetime.now(g.tz)
            delta = (after - before)
            msec = (delta.seconds * 1000 + delta.microseconds / 1000)
            if msec >= 100:
                g.log.warning("previous_visits code took %d msec" % msec)

        # check if we just came from the submit page
        infotext = None
        if request.get.get('already_submitted'):
            infotext = strings.already_submitted % article.resubmit_link()

        check_cheating('comments')

        if not c.user.pref_num_comments:
            num = g.num_comments
        elif c.user.gold:
            num = min(c.user.pref_num_comments, g.max_comments_gold)
        else:
            num = min(c.user.pref_num_comments, g.max_comments)

        kw = {}
        # allow depth to be reset (I suspect I'll turn the VInt into a
        # validator on my next pass of .compact)
        if depth is not None and 0 < depth < MAX_RECURSION:
            kw['max_depth'] = depth
        elif c.render_style == "compact":
            kw['max_depth'] = 5

        displayPane = PaneStack()

        # allow the user's total count preferences to be overwritten
        # (think of .embed as the use case together with depth=1)

        if limit and limit > 0:
            num = limit

        if c.user_is_loggedin and c.user.gold:
            if num > g.max_comments_gold:
                displayPane.append(InfoBar(message =
                                           strings.over_comment_limit_gold
                                           % max(0, g.max_comments_gold)))
                num = g.max_comments_gold
        elif num > g.max_comments:
            if limit:
                displayPane.append(InfoBar(message =
                                       strings.over_comment_limit
                                       % dict(max=max(0, g.max_comments),
                                              goldmax=max(0,
                                                   g.max_comments_gold))))
            num = g.max_comments

        # if permalink page, add that message first to the content
        if comment:
            displayPane.append(PermalinkMessage(article.make_permalink_slow()))

        displayPane.append(LinkCommentSep())

        # insert reply box only for logged in user
        if c.user_is_loggedin and can_comment_link(article) and not is_api():
            #no comment box for permalinks
            display = False
            if not comment:
                age = c.start_time - article._date
                if age.days < g.REPLY_AGE_LIMIT:
                    display = True
            displayPane.append(UserText(item = article, creating = True,
                                        post_form = 'comment',
                                        display = display,
                                        cloneable = True))

        if previous_visits:
            displayPane.append(CommentVisitsBox(previous_visits))
            # Used in later "more comments" renderings
            pv_hex = md5(repr(previous_visits)).hexdigest()
            g.cache.set(pv_hex, previous_visits, time=g.comment_visits_period)
            c.previous_visits_hex = pv_hex

        # Used in template_helpers
        c.previous_visits = previous_visits

        # finally add the comment listing
        displayPane.append(CommentPane(article, CommentSortMenu.operator(sort),
                                       comment, context, num, **kw))

        subtitle_buttons = []

        if c.focal_comment or context is not None:
            subtitle = None
        elif article.num_comments == 0:
            subtitle = _("no comments (yet)")
        elif article.num_comments <= num:
            subtitle = _("all %d comments") % article.num_comments
        else:
            subtitle = _("top %d comments") % num

            if g.max_comments > num:
                self._add_show_comments_link(subtitle_buttons, article, num,
                                             g.max_comments, gold=False)

            if (c.user_is_loggedin and c.user.gold
                and article.num_comments > g.max_comments):
                self._add_show_comments_link(subtitle_buttons, article, num,
                                             g.max_comments_gold, gold=True)

        res = LinkInfoPage(link = article, comment = comment,
                           content = displayPane,
                           subtitle = subtitle,
                           subtitle_buttons = subtitle_buttons,
                           nav_menus = [CommentSortMenu(default = sort)],
                           infotext = infotext).render()
        return res

    def _add_show_comments_link(self, array, article, num, max_comm, gold=False):
        if num == max_comm:
            return
        elif article.num_comments <= max_comm:
            link_text = _("show all %d") % article.num_comments
        else:
            link_text = _("show %d") % max_comm

        limit_param = "?limit=%d" % max_comm

        if gold:
            link_class = "gold"
        else:
            link_class = ""

        more_link = article.make_permalink_slow() + limit_param
        array.append( (link_text, more_link, link_class) )

    @validate(VUser(),
              name = nop('name'))
    def GET_newreddit(self, name):
        """Create a community form"""
        title = _('create a reddit')
        content=CreateSubreddit(name = name or '')
        res = FormPage(_("create a community"),
                       content = content,
                       ).render()
        return res

    def GET_stylesheet(self):
        if hasattr(c.site,'stylesheet_contents') and not g.css_killswitch:
            c.allow_loggedin_cache = True
            self.check_modified(c.site,'stylesheet_contents',
                                private=False, max_age=7*24*60*60,
                                must_revalidate=False)
            c.response_content_type = 'text/css'
            c.response.content =  c.site.stylesheet_contents
            return c.response
        else:
            return self.abort404()

    def _make_spamlisting(self, location, num, after, reverse, count):
        if location == 'reports':
            query = c.site.get_reported()
        elif location == 'spam':
            query = c.site.get_spam()
        elif location == 'trials':
            query = c.site.get_trials()
            num = 1000
        elif location == 'modqueue':
            query = c.site.get_modqueue()
        else:
            raise ValueError

        if isinstance(query, thing.Query):
            builder_cls = QueryBuilder
        elif isinstance (query, list):
            builder_cls = QueryBuilder
        else:
            builder_cls = IDBuilder

        def keep_fn(x):
            # no need to bother mods with banned users, or deleted content
            if getattr(x,'hidden',False) or x._deleted:
                return False

            if location == "reports":
                return x.reported > 0 and not x._spam
            elif location == "spam":
                return x._spam
            elif location == "trials":
                return not getattr(x, "verdict", None)
            elif location == "modqueue":
                if x.reported > 0 and not x._spam:
                    return True # reported but not banned
                verdict = getattr(x, "verdict", None)
                if verdict is None:
                    return True # anything without a verdict (i.e., trials)
                if x._spam and verdict != 'mod-removed':
                    return True # spam, unless banned by a moderator
                return False
            else:
                raise ValueError

        builder = builder_cls(query,
                              skip = True,
                              num = num, after = after,
                              keep_fn = keep_fn,
                              count = count, reverse = reverse,
                              wrap = ListingController.builder_wrapper)
        listing = LinkListing(builder)
        pane = listing.listing()

        return pane

    def _edit_modcontrib_reddit(self, location, num, after, reverse, count, created):
        extension_handling = False

        if not c.user_is_loggedin:
            return self.abort404()
        if isinstance(c.site, ModSR):
            level = 'mod'
        elif isinstance(c.site, ContribSR):
            level = 'contrib'
        elif isinstance(c.site, AllSR):
            level = 'all'
        else:
            raise ValueError

        if ((level == 'mod' and
             location in ('reports', 'spam', 'trials', 'modqueue'))
            or
            (level == 'all' and
             location == 'trials')):
            pane = self._make_spamlisting(location, num, after, reverse, count)
            if c.user.pref_private_feeds:
                extension_handling = "private"
        else:
            return self.abort404()

        return EditReddit(content = pane,
                          extension_handling = extension_handling).render()

    def _edit_normal_reddit(self, location, num, after, reverse, count, created):
        # moderator is either reddit's moderator or an admin
        is_moderator = c.user_is_loggedin and c.site.is_moderator(c.user) or c.user_is_admin
        extension_handling = False
        if is_moderator and location == 'edit':
            pane = PaneStack()
            if created == 'true':
                pane.append(InfoBar(message = strings.sr_created))
            pane.append(CreateSubreddit(site = c.site))
        elif location == 'moderators':
            pane = ModList(editable = is_moderator)
        elif is_moderator and location == 'banned':
            pane = BannedList(editable = is_moderator)
        elif (location == 'contributors' and
              # On public reddits, only moderators can see the whitelist.
              # On private reddits, all contributors can see each other.
              (c.site.type != 'public' or
               (c.user_is_loggedin and
                (c.site.is_moderator(c.user) or c.user_is_admin)))):
                pane = ContributorList(editable = is_moderator)
        elif (location == 'stylesheet'
              and c.site.can_change_stylesheet(c.user)
              and not g.css_killswitch):
            if hasattr(c.site,'stylesheet_contents_user') and c.site.stylesheet_contents_user:
                stylesheet_contents = c.site.stylesheet_contents_user
            elif hasattr(c.site,'stylesheet_contents') and c.site.stylesheet_contents:
                stylesheet_contents = c.site.stylesheet_contents
            else:
                stylesheet_contents = ''
            pane = SubredditStylesheet(site = c.site,
                                       stylesheet_contents = stylesheet_contents)
        elif location in ('reports', 'spam', 'trials', 'modqueue') and is_moderator:
            pane = self._make_spamlisting(location, num, after, reverse, count)
            if c.user.pref_private_feeds:
                extension_handling = "private"
        elif is_moderator and location == 'traffic':
            pane = RedditTraffic()
        elif c.user_is_sponsor and location == 'ads':
            pane = RedditAds()
        elif (not location or location == "about") and is_api():
            return Reddit(content = Wrapped(c.site)).render()
        else:
            return self.abort404()

        return EditReddit(content = pane,
                          extension_handling = extension_handling).render()

    @base_listing
    @validate(location = nop('location'),
              created = VOneOf('created', ('true','false'),
                               default = 'false'))
    def GET_editreddit(self, location, num, after, reverse, count, created):
        """Edit reddit form."""
        if isinstance(c.site, ModContribSR):
            return self._edit_modcontrib_reddit(location, num, after, reverse,
                                                count, created)
        elif isinstance(c.site, AllSR) and c.user_is_admin:
            return self._edit_modcontrib_reddit(location, num, after, reverse,
                                                count, created)
        elif isinstance(c.site, FakeSubreddit):
            return self.abort404()
        else:
            return self._edit_normal_reddit(location, num, after, reverse,
                                            count, created)


    def GET_awards(self):
        """The awards page."""
        return BoringPage(_("awards"), content = UserAwards()).render()

    # filter for removing punctuation which could be interpreted as lucene syntax
    related_replace_regex = re.compile('[?\\&|!{}+~^()":*-]+')
    related_replace_with  = ' '

    @base_listing
    @validate(article = VLink('article'))
    def GET_related(self, num, article, after, reverse, count):
        """Related page: performs a search using title of article as
        the search query."""

        if not can_view_link_comments(article):
            abort(403, 'forbidden')

        title = c.site.name + ((': ' + article.title) if hasattr(article, 'title') else '')

        query = self.related_replace_regex.sub(self.related_replace_with,
                                               article.title)
        if len(query) > 1024:
            # could get fancier and break this into words, but titles
            # longer than this are typically ascii art anyway
            query = query[0:1023]

        q = RelatedSearchQuery(query, ignore = [article._fullname])
        num, t, pane = self._search(q,
                                    num = num, after = after, reverse = reverse,
                                    count = count)

        return LinkInfoPage(link = article, content = pane,
                            subtitle = _('related')).render()

    @base_listing
    @validate(article = VLink('article'))
    def GET_duplicates(self, article, num, after, reverse, count):
        if not can_view_link_comments(article):
            abort(403, 'forbidden')

        links = link_duplicates(article)
        builder = IDBuilder([ link._fullname for link in links ],
                            num = num, after = after, reverse = reverse,
                            count = count, skip = False)
        listing = LinkListing(builder).listing()

        res = LinkInfoPage(link = article,
                           comment = None,
                           duplicates = links,
                           content = listing,
                           subtitle = _('other discussions')).render()
        return res


    @base_listing
    @validate(query = nop('q'))
    def GET_search_reddits(self, query, reverse, after,  count, num):
        """Search reddits by title and description."""
        q = SubredditSearchQuery(query)

        num, t, spane = self._search(q, num = num, reverse = reverse,
                                     after = after, count = count)
        
        res = SubredditsPage(content=spane,
                             prev_search = query,
                             elapsed_time = t,
                             num_results = num,
                             # update if we ever add sorts
                             search_params = {},
                             title = _("search results"),
                             simple=True).render()
        return res

    verify_langs_regex = re.compile(r"\A[a-z][a-z](,[a-z][a-z])*\Z")
    @base_listing
    @validate(query = nop('q'),
              sort = VMenu('sort', SearchSortMenu, remember=False),
              restrict_sr = VBoolean('restrict_sr', default=False))
    def GET_search(self, query, num, reverse, after, count, sort, restrict_sr):
        """Search links page."""
        if query and '.' in query:
            url = sanitize_url(query, require_scheme = True)
            if url:
                return self.redirect("/submit" + query_string({'url':url}))

        if not restrict_sr:
            site = DefaultSR()
        else:
            site = c.site

        try:
            cleanup_message = None
            try:
                q = IndextankQuery(query, site, sort)
                num, t, spane = self._search(q, num=num, after=after, 
                                             reverse = reverse, count = count)
            except InvalidIndextankQuery:
                # strip the query down to a whitelist
                cleaned = re.sub("[^\w\s]+", "", query)
                cleaned = cleaned.lower()

                # if it was nothing but mess, we have to stop
                if not cleaned.strip():
                    num, t, spane = 0, 0, []
                    cleanup_message = strings.completely_invalid_search_query
                else:
                    q = IndextankQuery(cleaned, site, sort)
                    num, t, spane = self._search(q, num=num, after=after, 
                                                 reverse=reverse, count=count)
                    cleanup_message = strings.invalid_search_query % {
                                          "clean_query": cleaned
                                      }
		
            res = SearchPage(_('search results'), query, t, num, content=spane,
                             nav_menus = [SearchSortMenu(default=sort)],
                             search_params = dict(sort = sort), 
                             infotext=cleanup_message,
                             simple=False, site=c.site, 
                             restrict_sr=restrict_sr).render()

            return res
        except (IndextankException, socket.error), e:
            return self.search_fail(e)

    def _search(self, query_obj, num, after, reverse, count=0):
        """Helper function for interfacing with search.  Basically a
           thin wrapper for SearchBuilder."""

        builder = SearchBuilder(query_obj,
                                after = after, num = num, reverse = reverse,
                                count = count,
                                wrap = ListingController.builder_wrapper)

        listing = LinkListing(builder, show_nums=True)

        # have to do it in two steps since total_num and timing are only
        # computed after fetch_more
        try:
            res = listing.listing()
        except (IndextankException, SolrError, socket.error), e:
            return self.search_fail(e)
        timing = time_module.time() - builder.start_time

        return builder.total_num, timing, res

    @validate(VAdmin(),
              comment = VCommentByID('comment_id'))
    def GET_comment_by_id(self, comment):
        href = comment.make_permalink_slow(context=5, anchor=True)
        return self.redirect(href)

    @validate(url = VRequired('url', None),
              title = VRequired('title', None),
              then = VOneOf('then', ('tb','comments'), default = 'comments'))
    def GET_submit(self, url, title, then):
        """Submit form."""
        if url and not request.get.get('resubmit'):
            # check to see if the url has already been submitted
            links = link_from_url(url)
            if links and len(links) == 1:
                return self.redirect(links[0].already_submitted_link)
            elif links:
                infotext = (strings.multiple_submitted
                            % links[0].resubmit_link())
                res = BoringPage(_("seen it"),
                                 content = wrap_links(links),
                                 infotext = infotext).render()
                return res

        if not c.user_is_loggedin:
            raise UserRequiredException

        if not (c.default_sr or c.site.can_submit(c.user)):
            abort(403, "forbidden")

        captcha = Captcha() if c.user.needs_captcha() else None
        sr_names = (Subreddit.submit_sr_names(c.user) or
                    Subreddit.submit_sr_names(None))

        return FormPage(_("submit"),
                        show_sidebar = True,
                        content=NewLink(url=url or '',
                                        title=title or '',
                                        subreddits = sr_names,
                                        captcha=captcha,
                                        then = then)).render()

    def GET_frame(self):
        """used for cname support.  makes a frame and
        puts the proper url as the frame source"""
        sub_domain = request.environ.get('sub_domain')
        original_path = request.environ.get('original_path')
        sr = Subreddit._by_domain(sub_domain)
        return Cnameframe(original_path, sr, sub_domain).render()


    def GET_framebuster(self, what = None, blah = None):
        """
        renders the contents of the iframe which, on a cname, checks
        if the user is currently logged into reddit.
        
        if this page is hit from the primary domain, redirects to the
        cnamed domain version of the site.  If the user is logged in,
        this cnamed version will drop a boolean session cookie on that
        domain so that subsequent page reloads will be caught in
        middleware and a frame will be inserted around the content.

        If the user is not logged in, previous session cookies will be
        emptied so that subsequent refreshes will not be rendered in
        that pesky frame.
        """
        if not c.site.domain:
            return ""
        elif c.cname:
            return FrameBuster(login = (what == "login")).render()
        else:
            path = "/framebuster/"
            if c.user_is_loggedin:
                path += "login/"
            u = UrlParser(path + str(random.random()))
            u.mk_cname(require_frame = False, subreddit = c.site,
                       port = request.port)
            return self.redirect(u.unparse())
        # the user is not logged in or there is no cname.
        return FrameBuster(login = False).render()

    def GET_catchall(self):
        return self.abort404()

    @validate(period = VInt('seconds',
                            min = sup.MIN_PERIOD,
                            max = sup.MAX_PERIOD,
                            default = sup.MIN_PERIOD))
    def GET_sup(self, period):
        #dont cache this, it's memoized elsewhere
        c.used_cache = True
        sup.set_expires_header()

        if c.extension == 'json':
            c.response.content = sup.sup_json(period)
            return c.response
        else:
            return self.abort404()


    @validate(VTrafficViewer('article'),
              article = VLink('article'))
    def GET_traffic(self, article):
        content = PromotedTraffic(article)
        if c.render_style == 'csv':
            c.response.content = content.as_csv()
            return c.response

        return LinkInfoPage(link = article,
                           comment = None,
                           content = content).render()

    @validate(VSponsorAdmin())
    def GET_site_traffic(self):
        return BoringPage("traffic",
                          content = RedditTraffic()).render()

class FormsController(RedditController):

    def GET_password(self):
        """The 'what is my password' page"""
        return BoringPage(_("password"), content=Password()).render()

    @validate(VUser(),
              dest = VDestination(),
              reason = nop('reason'))
    def GET_verify(self, dest, reason):
        if c.user.email_verified:
            content = InfoBar(message = strings.email_verified)
            if dest:
                return self.redirect(dest)
        else:
            if reason == "submit":
                infomsg = strings.verify_email_submit
            else:
                infomsg = strings.verify_email

            content = PaneStack(
                [InfoBar(message = infomsg),
                 PrefUpdate(email = True, verify = True,
                            password = False)])
        return BoringPage(_("verify email"), content = content).render()

    @validate(VUser(),
              cache_evt = VCacheKey('email_verify', ('key',)),
              key = nop('key'),
              dest = VDestination(default = "/prefs/update"))
    def GET_verify_email(self, cache_evt, key, dest):
        if c.user_is_loggedin and c.user.email_verified:
            cache_evt.clear()
            return self.redirect(dest)
        elif not (cache_evt.user and
                key == passhash(cache_evt.user.name, cache_evt.user.email)):
            content = PaneStack(
                [InfoBar(message = strings.email_verify_failed),
                 PrefUpdate(email = True, verify = True,
                            password = False)])
            return BoringPage(_("verify email"), content = content).render()
        elif c.user != cache_evt.user:
            # wrong user.  Log them out and try again. 
            self.logout()
            return self.redirect(request.fullpath)
        else:
            cache_evt.clear()
            c.user.email_verified = True
            c.user._commit()
            Award.give_if_needed("verified_email", c.user)
            return self.redirect(dest)

    @validate(cache_evt = VHardCacheKey('email-reset', ('key',)),
              key = nop('key'))
    def GET_resetpassword(self, cache_evt, key):
        """page hit once a user has been sent a password reset email
        to verify their identity before allowing them to update their
        password."""

        #if another user is logged-in, log them out
        if c.user_is_loggedin:
            self.logout()
            return self.redirect(request.path)

        done = False
        if not key and request.referer:
            referer_path = request.referer.split(g.domain)[-1]
            done = referer_path.startswith(request.fullpath)
        elif not getattr(cache_evt, "user", None):
            return self.redirect("/password?expired=true")
        return BoringPage(_("reset password"),
                          content=ResetPassword(key=key, done=done)).render()

    @validate(VUser())
    def GET_depmod(self):
        displayPane = PaneStack()

        active_trials = {}
        finished_trials = {}

        juries = Jury.by_account(c.user)

        trials = trial_info([j._thing2 for j in juries])

        for j in juries:
            defendant = j._thing2

            if trials.get(defendant._fullname, False):
                active_trials[defendant._fullname] = j._name
            else:
                finished_trials[defendant._fullname] = j._name

        if active_trials:
            fullnames = sorted(active_trials.keys(), reverse=True)

            def my_wrap(thing):
                w = Wrapped(thing)
                w.hide_score = True
                w.likes = None
                w.trial_mode = True
                w.render_class = LinkOnTrial
                w.juryvote = active_trials[thing._fullname]
                return w

            listing = wrap_links(fullnames, wrapper=my_wrap)
            displayPane.append(InfoBar(strings.active_trials,
                                       extra_class="mellow"))
            displayPane.append(listing)

        if finished_trials:
            fullnames = sorted(finished_trials.keys(), reverse=True)
            listing = wrap_links(fullnames)
            displayPane.append(InfoBar(strings.finished_trials,
                                       extra_class="mellow"))
            displayPane.append(listing)

        displayPane.append(InfoBar(strings.more_info_link %
                                       dict(link="/help/deputies"),
                                   extra_class="mellow"))

        return Reddit(content = displayPane).render()

    @validate(VUser(),
              location = nop("location"))
    def GET_prefs(self, location=''):
        """Preference page"""
        content = None
        infotext = None
        if not location or location == 'options':
            content = PrefOptions(done=request.get.get('done'))
        elif location == 'friends':
            content = PaneStack()
            infotext = strings.friends % Friends.path
            content.append(FriendList())
            content.append(EnemyList())
        elif location == 'update':
            content = PrefUpdate()
        elif location == 'feeds' and c.user.pref_private_feeds:
            content = PrefFeeds()
        elif location == 'delete':
            content = PrefDelete()
        else:
            return self.abort404()

        return PrefsPage(content = content, infotext=infotext).render()


    @validate(dest = VDestination())
    def GET_login(self, dest):
        """The /login form.  No link to this page exists any more on
        the site (all actions invoking it now go through the login
        cover).  However, this page is still used for logging the user
        in during submission or voting from the bookmarklets."""

        if (c.user_is_loggedin and
            not request.environ.get('extension') == 'embed'):
            return self.redirect(dest)
        return LoginPage(dest = dest).render()


    @validate(dest = VDestination())
    def GET_register(self, dest):
        if (c.user_is_loggedin and
            not request.environ.get('extension') == 'embed'):
            return self.redirect(dest)
        return RegisterPage(dest = dest).render()

    @validate(VUser(),
              VModhash(),
              dest = VDestination())
    def GET_logout(self, dest):
        return self.redirect(dest)

    @validate(VUser(),
              VModhash(),
              dest = VDestination())
    def POST_logout(self, dest):
        """wipe login cookie and redirect to referer."""
        self.logout()
        return self.redirect(dest)


    @validate(VUser(),
              dest = VDestination())
    def GET_adminon(self, dest):
        """Enable admin interaction with site"""
        #check like this because c.user_is_admin is still false
        if not c.user.name in g.admins:
            return self.abort404()
        self.login(c.user, admin = True, rem = True)
        return self.redirect(dest)

    @validate(VAdmin(),
              dest = VDestination())
    def GET_adminoff(self, dest):
        """disable admin interaction with site."""
        if not c.user.name in g.admins:
            return self.abort404()
        self.login(c.user, admin = False, rem = True)
        return self.redirect(dest)

    def GET_validuser(self):
        """checks login cookie to verify that a user is logged in and
        returns their user name"""
        c.response_content_type = 'text/plain'
        if c.user_is_loggedin:
            perm = str(g.allow_wiki_editing and c.user.can_wiki())
            c.response.content = c.user.name + "," + perm
        else:
            c.response.content = ''
        return c.response

    def _render_opt_in_out(self, msg_hash, leave):
        """Generates the form for an optin/optout page"""
        email = Email.handler.get_recipient(msg_hash)
        if not email:
            return self.abort404()
        sent = (has_opted_out(email) == leave)
        return BoringPage(_("opt out") if leave else _("welcome back"),
                          content = OptOut(email = email, leave = leave, 
                                           sent = sent, 
                                           msg_hash = msg_hash)).render()

    @validate(msg_hash = nop('x'))
    def GET_optout(self, msg_hash):
        """handles /mail/optout to add an email to the optout mailing
        list.  The actual email addition comes from the user posting
        the subsequently rendered form and is handled in
        ApiController.POST_optout."""
        return self._render_opt_in_out(msg_hash, True)

    @validate(msg_hash = nop('x'))
    def GET_optin(self, msg_hash):
        """handles /mail/optin to remove an email address from the
        optout list. The actual email removal comes from the user
        posting the subsequently rendered form and is handled in
        ApiController.POST_optin."""
        return self._render_opt_in_out(msg_hash, False)

    @validate(dest = VDestination("dest"))
    def GET_try_compact(self, dest):
        c.render_style = "compact"
        return TryCompact(dest = dest).render()

    @validate(VUser(),
              secret=VPrintable("secret", 50))
    def GET_thanks(self, secret):
        """The page to claim reddit gold trophies"""
        return BoringPage(_("thanks"), content=Thanks(secret)).render()

    @validate(VUser(),
              goldtype = VOneOf("goldtype",
                                ("autorenew", "onetime", "creddits", "gift")),
              period = VOneOf("period", ("monthly", "yearly")),
              months = VInt("months"),
              # variables below are just for gifts
              signed = VBoolean("signed"),
              recipient_name = VPrintable("recipient", max_length = 50),
              giftmessage = VLength("giftmessage", 10000))
    def GET_gold(self, goldtype, period, months,
                 signed, recipient_name, giftmessage):
        start_over = False
        recipient = None
        if goldtype == "autorenew":
            if period is None:
                start_over = True
        elif goldtype in ("onetime", "creddits"):
            if months is None or months < 1:
                start_over = True
        elif goldtype == "gift":
            if months is None or months < 1:
                start_over = True
            try:
                recipient = Account._by_name(recipient_name or "")
            except NotFound:
                start_over = True
        else:
            goldtype = ""
            start_over = True

        if start_over:
            return BoringPage(_("reddit gold"),
                              show_sidebar = False,
                              content=Gold(goldtype, period, months, signed,
                                           recipient, recipient_name)).render()
        else:
            payment_blob = dict(goldtype     = goldtype,
                                account_id   = c.user._id,
                                account_name = c.user.name,
                                status       = "initialized")

            if goldtype == "gift":
                payment_blob["signed"] = signed
                payment_blob["recipient"] = recipient_name
                payment_blob["giftmessage"] = giftmessage

            passthrough = randstr(15)

            g.hardcache.set("payment_blob-" + passthrough,
                            payment_blob, 86400 * 30)

            g.log.info("just set payment_blob-%s" % passthrough)

            return BoringPage(_("reddit gold"),
                              show_sidebar = False,
                              content=GoldPayment(goldtype, period, months,
                                                  signed, recipient,
                                                  giftmessage, passthrough)
                              ).render()


