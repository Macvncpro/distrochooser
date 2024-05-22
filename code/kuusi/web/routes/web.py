"""
kuusi
Copyright (C) 2014-2024  Christoph Müller  <mail@chmr.eu>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
from typing import List, Tuple
from django.http import (
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect
)
from django.http import Http404
from django.template import loader
from django.utils.translation import gettext_lazy as _
from django.utils import translation
from os.path import join

from kuusi.settings import (
    KUUSI_NAME,
    ACCELERATION,
    DEBUG,
    LANGUAGE_CODES,
    DEFAULT_LANGUAGE_CODE,
    KUUSI_TRANSLATION_URL
)
from web.models import Page, Session, WebHttpRequest, Category, FacetteSelection, Choosable, ChoosableMeta
from web.helper import forward_helper
from web.models.translateable import INCOMPLETE_TRANSLATIONS
from logging import getLogger

logger = getLogger("root")


def get_page_route(page: Page) -> Tuple[Page | None, List[Page]]: 
    """
    Get a next page and all available pages from the given page as a start point
    """
    pages = []
    prev_page = page.previous_page
    while prev_page is not None:
        if prev_page:
            pages = [prev_page] + pages
            prev_page = prev_page.previous_page
        else:
            prev_page = None
    pages.append(page)
    next_page = page.next_page
    while next_page is not None:
        if next_page:
            pages.append(next_page)
            next_page = next_page.next_page
        else:
            next_page = None
    
    return next_page, pages

def get_session(page: Page, request: WebHttpRequest) -> Session:
    # Get a session object based on the informations present. If no result_id is existing withing the session a new session will be started
    # FIXME: Get rid of the cookie
    session: Session = None
    if page.require_session:
        # TODO: Make the handling better. TO pick up old results it's required to have a session, but the welcome page should not feature a session due to cookies
        # TODO: Decide if when no id is given -> new session or not?
        # TODO: Also, get rid of the csrftoken cookie until user gave consent
        if "result_id" not in request.session:
            session = get_fresh_session(request)
        else:
            session = Session.objects.filter(
                result_id=request.session["result_id"]
            ).first()
            # TODO: Add some display to indicate what happens to old sessions
            if session.valid_for != "latest": 
                # The session is linking to some old result version
                session = get_fresh_session(request)

    request.session["result_id"] = session.result_id
    return session

def get_fresh_session(request: WebHttpRequest) -> Session:
    user_agent = request.headers.get("user-agent")
    session = Session(user_agent=user_agent)
    session.save()
    session.referrer = request.headers.get("referrer")
    return session

def clone_selections(id: str, request: WebHttpRequest, session: Session):
    old_session = Session.objects.filter(result_id=id).first()
    if old_session:
        logger.debug(f"Found old session {old_session}")
        if not session.session_origin:
            selections = FacetteSelection.objects.filter(session=old_session)
            selection: FacetteSelection
            for selection in selections:
                # prevent double copies
                if (
                    FacetteSelection.objects.filter(
                        session=session, facette=selection.facette
                    ).count()
                    == 0
                ):
                    selection.pk = None
                    selection.session = session
                    selection.save()
            # Make sure that copied results do not leave the version
            session.valid_for = old_session.valid_for
            session.session_origin = old_session
            session.save()
        else:
            if session.session_origin != old_session:
                logger.debug(f"This is a new session, but the user has a session.")
                # TODO: Create a new session in case the user clicks on another session link.
                # TODO: Get rid of redundancy with  above
                user_agent = request.headers.get("user-agent")
                session = Session(user_agent=user_agent, session_origin=old_session)
                session.save()
                request.session["result_id"] = session.result_id
            else:
                logger.debug(
                    f"Skipping selection copy, the session {session} is already linked to session {old_session}"
                )

def get_categories_and_filtered_pages(pages: List[Page], session: Session) -> Tuple[List[Page], List[Category]]: 
    # Get Categories and pages suitable for the currently existing session
    version_comp_pages = []
    chained_page: Page
    for chained_page in pages:
        if chained_page.is_visible(session):
            version_comp_pages.append(chained_page)

    categories = []
    for chained_page in pages:
        # Child categories will be created later, when the steps are created.
        used_in_category = Category.objects.filter(
            target_page=chained_page, child_of__isnull=True
        )
        if used_in_category.count() > 0:
            categories.append(used_in_category.first())
    return version_comp_pages, categories

def build_step_data(categories: List[Category], request: WebHttpRequest):
    step_data = []
    index: int
    category: Category
    for index, category in enumerate(categories):
        minor_steps = []
        category_step = category.to_step(
            request,
            index == categories.__len__() - 1,
        )
        child_categories = Category.objects.filter(child_of=category)
        child_category: Category
        for child_category in child_categories:
            minor_steps.append(
                child_category.to_step(
                    request,
                )
            )
        step = {
            "icon": category.icon,
            "major": category_step,
            "minor": minor_steps,
        }
        step_data.append(step)
    return step_data

def route_outgoing(request: WebHttpRequest, id: int, property: str) -> HttpResponse:
    got = Choosable.objects.filter(pk=id) 
    property = property.upper()
    if got.count() == 1:
        choosable: Choosable = got.first()
        if choosable:
            if property not in choosable.meta:
                raise Http404()
            else:
                choosable.clicked += 1 # FIXME: Todo consider click related value to allow map property -> click
                choosable.save()
                return HttpResponseRedirect(choosable.meta[property].meta_value)
    raise Http404()

def route_index(request: WebHttpRequest, language_code: str = None, id: str = None):
    template = loader.get_template("index.html")

    # Get the current page
    page_id = request.GET.get("page")
    page = None
    if page_id:
        page = Page.objects.get(catalogue_id=page_id, is_invalidated=False)
    else:
        page = Page.objects.filter(is_invalidated=False).first()

    # get the categories in an order fitting the pages
    _, pages = get_page_route(page)

    session = get_session(page, request)

    if id is not None and session:
        # Load selections of an old session, if needed
        clone_selections(id, request, session)

    # Onboard th session to the request oject 

    # TODO: If the user accesses the site with a GET parameter result_id, create a new session and copy old results.
    # TODO: Prevent that categories are disappearing due to missing session on the first page
    request.session_obj = session    
    # TODO: These are not properly set within WebHttpRequest class.
    request.has_errors = False
    request.has_warnings = False
    # TODO: Investigate correct approach

    # i18n handling
    request.LANGUAGE_CODE = (
        DEFAULT_LANGUAGE_CODE if not language_code else language_code
    )
    if session.language_code != request.LANGUAGE_CODE:
        logger.debug(f"Session lang was {session.language_code} is now {request.LANGUAGE_CODE}")
        session.language_code = request.LANGUAGE_CODE
        session.save()
    translation.activate(request.LANGUAGE_CODE)

    # Build the navigation/ Categories
    pages, categories = get_categories_and_filtered_pages(pages, session)

    # Turbo call handling
    overwrite_status = 200
    base_url = f"/{request.LANGUAGE_CODE}" + ("" if not id else f"/{id}")
    if request.method == "POST":
        overwrite_status, response = forward_helper(
            id, overwrite_status, session, base_url, page, request
        )
        if response and not overwrite_status:
            return response
        
    
    current_location = request.get_full_path()
    # If the user is curently on the start page -> use the first available site as "current location"
    if current_location.__len__() <= 1:
        current_location = base_url + pages[0].href

    """
    In Case the desired page is not active within the current version -> attempt to find the next one suitable

    If not page is suitable, it will result in a 405 later.
    """
    if not page.is_visible(session):
        page = Page.next_visible_page(page, session)

    step_data = build_step_data(categories, request)

    if not page.is_visible(request.session_obj):
        return HttpResponseNotAllowed(_("PAGE_NOT_AVAILABLE"))

    context = {
        "title": KUUSI_NAME,
        "page": page,
        "steps": step_data,
        "acceleration": ACCELERATION,
        "debug": DEBUG,
        "language_codes": LANGUAGE_CODES,
        "language_code": request.LANGUAGE_CODE,
        "session": session,
        "is_old": session.valid_for != "latest",
        "locale_incomplete": language_code in INCOMPLETE_TRANSLATIONS,
        "translation_url": KUUSI_TRANSLATION_URL
    }

    if "accept" in request.headers and "turbo" in request.headers.get("accept"):
        logger.debug(f"This is a turbo call")
    else:
        overwrite_status = 200
        logger.debug(
            f"There is no ext/vnd.turbo-stream.html accept header. Revoking all status code changes."
        )

    logger.debug(f"Status overwrite is {overwrite_status}")

    return HttpResponse(template.render(context, request), status=overwrite_status)

