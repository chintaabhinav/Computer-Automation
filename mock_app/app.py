"""Legacy-flavored mock bank back-office app used as the local target for the computer-use agent.

This is a deliberately old-fashioned, three-page intranet workflow (member search -> member
detail -> sub-account confirmation) built to be *hard* for an automation agent in the ways real
enterprise apps are hard: table-based layout, inline styles, non-semantic divs and spans, no
test IDs and no ``data-*`` hooks, and a savings balance buried in a nested table so it cannot be
scraped by a lucky single selector. Elements are identifiable only by their visible text, their
form labels, or their position in the document structure -- which is exactly the perception
problem the agent's surface layer has to solve, and the reason we do not want a clean, modern,
easily-selectable target here. Every route also honors an ``?inject=`` query parameter
(``not_found``, ``slow``, ``popup``, ``session_expired``, ``server_error``) so discovery and
replay runs can reproduce exceptional states on demand instead of waiting for them to happen by
chance; that makes the error taxonomy testable and the failure paths part of the regular test
suite. All member data is hardcoded fake data -- no database, no login, no real PII, no secrets.
"""

import time

from flask import Flask, redirect, request, url_for

app = Flask(__name__)

PORT = 5001

# Fake members. Nothing here is real: invented names, invented numbers, no PII.
MEMBERS = {
    "12345": {
        "name": "Jane Doe",
        "branch": "Northgate Main",
        "opened": "04/17/1998",
        "savings_account": "000-12345-01",
        "available": "4,200.00",
        "ledger": "4,200.00",
        "pending": "0.00",
    },
    "67890": {
        "name": "Robert Chen",
        "branch": "Elmwood",
        "opened": "11/02/2006",
        "savings_account": "000-67890-01",
        "available": "18,750.35",
        "ledger": "18,900.35",
        "pending": "150.00",
    },
    "24680": {
        "name": "Maria Alvarez",
        "branch": "Northgate Main",
        "opened": "07/28/2019",
        "savings_account": "000-24680-01",
        "available": "312.09",
        "ledger": "312.09",
        "pending": "0.00",
    },
}

INJECTIONS = ("not_found", "slow", "popup", "session_expired", "server_error")

SLOW_SECONDS = 5


# --------------------------------------------------------------------------------------
# Legacy chrome. Deliberately inline-styled, table-based, and free of automation hooks.
# --------------------------------------------------------------------------------------


def page(title, body):
    return (
        "<html><head><title>NMCU Back Office - "
        + title
        + "</title></head>"
        '<body bgcolor="#d4d0c8" style="margin:0;font-family:Verdana,Arial,sans-serif;'
        'font-size:12px;color:#000000;">'
        '<table width="100%" border="0" cellpadding="0" cellspacing="0">'
        '<tr><td bgcolor="#003366" style="padding:6px 10px;">'
        '<font color="#ffffff" face="Verdana" size="2"><b>NORTHGATE MUTUAL CREDIT UNION</b>'
        "</font><br>"
        '<font color="#c8d8e8" face="Verdana" size="1">Back Office Terminal &nbsp;|&nbsp; '
        "Rel. 4.2.1 &nbsp;|&nbsp; Operator: OPERATOR-07 &nbsp;|&nbsp; Branch: 014</font>"
        "</td></tr>"
        '<tr><td bgcolor="#8a9aa8" style="padding:3px 10px;">'
        '<font face="Verdana" size="1" color="#ffffff">Member Services &gt; Savings &gt; '
        "Sub-Accounts</font></td></tr>"
        "</table>"
        '<div style="padding:14px 18px;">' + body + "</div>"
        '<table width="100%" border="0" cellpadding="4" cellspacing="0" '
        'style="margin-top:24px;border-top:1px solid #808080;">'
        '<tr><td><font face="Verdana" size="1" color="#404040">'
        "Internal use only. Test system -- fictitious data. Do not enter live member "
        "information.</font></td></tr></table>"
        "</body></html>"
    )


def inject_of(req):
    """Read the injected condition off the query string or the posted form."""
    value = (req.values.get("inject") or "").strip()
    return value if value in INJECTIONS else ""


def carry(inject):
    """Hidden field that keeps the injected condition alive across a form submission."""
    if not inject:
        return ""
    return '<input type="hidden" name="inject" value="' + inject + '">'


def qs(inject, **params):
    """Build a query string for links, preserving the injected condition."""
    parts = [k + "=" + str(v) for k, v in params.items() if v]
    if inject:
        parts.append("inject=" + inject)
    return ("?" + "&".join(parts)) if parts else ""


def notice_box(heading, message, link_text, link_href):
    return (
        '<table border="1" cellpadding="10" cellspacing="0" bordercolor="#808080" '
        'bgcolor="#ffffff" width="520"><tr><td>'
        '<font face="Verdana" size="2" color="#990000"><b>' + heading + "</b></font>"
        '<div style="margin-top:8px;"><font face="Verdana" size="2">' + message
        + "</font></div>"
        '<div style="margin-top:14px;"><a href="' + link_href + '">'
        '<font face="Verdana" size="2">' + link_text + "</font></a></div>"
        "</td></tr></table>"
    )


# --------------------------------------------------------------------------------------
# Injected states shared by every route.
# --------------------------------------------------------------------------------------


def session_expired_page():
    return page(
        "Session Expired",
        notice_box(
            "Session Ended",
            "Your session has expired, please log in again.",
            "Return to Log In",
            url_for("search"),
        ),
    )


def server_error_page():
    return page(
        "Error",
        '<table border="1" cellpadding="10" cellspacing="0" bordercolor="#808080" '
        'bgcolor="#ffffff" width="520"><tr><td>'
        '<font face="Verdana" size="2" color="#990000"><b>HTTP 500 - Internal Server Error'
        "</b></font>"
        '<div style="margin-top:8px;"><font face="Verdana" size="2">'
        "The transaction could not be completed. Reference NMCU-CORE-0x5F. Contact the "
        "help desk if this condition persists.</font></div>"
        "</td></tr></table>",
    )


def preflight(inject):
    """Apply the injected conditions that short-circuit a request. Returns a response or None."""
    if inject == "slow":
        time.sleep(SLOW_SECONDS)
    if inject == "session_expired":
        return session_expired_page()
    if inject == "server_error":
        return server_error_page(), 500
    return None


# --------------------------------------------------------------------------------------
# Page 1 -- member search.
# --------------------------------------------------------------------------------------


@app.route("/")
def search():
    inject = inject_of(request)
    early = preflight(inject)
    if early is not None:
        return early

    return page(
        "Member Search",
        '<font face="Verdana" size="2"><b>Member Inquiry</b></font>'
        '<div style="margin:6px 0 12px 0;"><font face="Verdana" size="1" color="#404040">'
        "Enter a member identifier to retrieve the savings relationship.</font></div>"
        '<form method="get" action="' + url_for("member") + '">'
        + carry(inject)
        + '<table border="1" cellpadding="8" cellspacing="0" bordercolor="#808080" '
        'bgcolor="#ffffff"><tr>'
        '<td bgcolor="#eceae4"><label for="mid"><font face="Verdana" size="2">'
        "Member ID</font></label></td>"
        '<td><input type="text" id="mid" name="member_id" size="18" maxlength="12" '
        'style="font-family:Courier New,monospace;font-size:12px;border:1px solid #808080;">'
        "</td>"
        '<td><input type="submit" value="Search" '
        'style="font-family:Verdana;font-size:11px;padding:2px 12px;"></td>'
        "</tr></table></form>"
        '<div style="margin-top:16px;"><font face="Verdana" size="1" color="#404040">'
        "Sample identifiers on this test region: 12345, 67890, 24680</font></div>",
    )


# --------------------------------------------------------------------------------------
# Page 2 -- member detail, with the balance nested one table deep.
# --------------------------------------------------------------------------------------


def maintenance_overlay():
    """Interstitial that covers the page until it is dismissed. No id, no data attributes."""
    return (
        '<div style="position:fixed;top:0;left:0;width:100%;height:100%;'
        'background-color:rgba(0,0,0,0.45);z-index:9999;">'
        '<table border="0" cellpadding="0" cellspacing="0" width="100%" height="100%">'
        '<tr><td align="center" valign="middle">'
        '<table border="2" cellpadding="14" cellspacing="0" bordercolor="#003366" '
        'bgcolor="#ffffe1" width="440"><tr><td>'
        '<font face="Verdana" size="2"><b>System Notice</b></font>'
        '<div style="margin-top:10px;"><font face="Verdana" size="2">'
        "Scheduled core maintenance is planned for Sunday 02:00-04:00. Sub-account openings "
        "posted during the window will settle the following business day."
        "</font></div>"
        "</td></tr>"
        # The button sits in its own cell, not a div, so the walk below lands on the overlay.
        '<tr><td align="right">'
        '<input type="button" value="Continue" '
        'style="font-family:Verdana;font-size:11px;padding:2px 14px;" '
        "onclick=\"var n=this;while(n&&n.tagName!='DIV')n=n.parentNode;"
        'if(n){n.style.display=\'none\';}">'
        "</td></tr></table>"
        "</td></tr></table>"
        "</div>"
    )


@app.route("/member")
def member():
    inject = inject_of(request)
    early = preflight(inject)
    if early is not None:
        return early

    member_id = (request.values.get("member_id") or "").strip()
    record = None if inject == "not_found" else MEMBERS.get(member_id)

    if record is None:
        return page(
            "Member Not Found",
            notice_box(
                "No Such Member",
                "No such member could be located for identifier <b>"
                + (member_id if member_id else "(blank)")
                + "</b>. Verify the identifier and try the inquiry again.",
                "Back to Member Inquiry",
                url_for("search") + qs(inject),
            ),
        )

    balance_table = (
        '<table border="1" cellpadding="4" cellspacing="0" bordercolor="#a0a0a0" '
        'bgcolor="#fbfbf7"><tr>'
        '<td><font face="Verdana" size="1">Available</font></td>'
        '<td align="right"><font face="Courier New" size="2"><b>$'
        + record["available"]
        + "</b></font></td></tr>"
        '<tr><td><font face="Verdana" size="1">Ledger</font></td>'
        '<td align="right"><font face="Courier New" size="2">$'
        + record["ledger"]
        + "</font></td></tr>"
        '<tr><td><font face="Verdana" size="1">Holds / Pending</font></td>'
        '<td align="right"><font face="Courier New" size="2">$'
        + record["pending"]
        + "</font></td></tr></table>"
    )

    detail = (
        '<font face="Verdana" size="2"><b>Member Detail</b></font>'
        '<table border="1" cellpadding="8" cellspacing="0" bordercolor="#808080" '
        'bgcolor="#ffffff" width="620" style="margin-top:8px;">'
        '<tr><td bgcolor="#eceae4" width="180"><font face="Verdana" size="2">Member Name'
        "</font></td>"
        '<td><span style="font-weight:bold;">' + record["name"] + "</span></td></tr>"
        '<tr><td bgcolor="#eceae4"><font face="Verdana" size="2">Member ID</font></td>'
        '<td><span style="font-family:Courier New,monospace;">' + member_id
        + "</span></td></tr>"
        '<tr><td bgcolor="#eceae4"><font face="Verdana" size="2">Home Branch</font></td>'
        "<td><span>" + record["branch"] + "</span></td></tr>"
        '<tr><td bgcolor="#eceae4"><font face="Verdana" size="2">Relationship Opened</font>'
        "</td><td><span>" + record["opened"] + "</span></td></tr>"
        '<tr><td bgcolor="#eceae4" valign="top"><font face="Verdana" size="2">'
        "Savings Balance</font></td>"
        '<td valign="top">'
        '<div style="margin-bottom:4px;"><font face="Verdana" size="1" color="#404040">'
        "Acct " + record["savings_account"] + " &nbsp;|&nbsp; USD</font></div>"
        + balance_table
        + "</td></tr>"
        "</table>"
        '<form method="post" action="' + url_for("confirm") + qs(inject) + '" '
        'style="margin-top:16px;">'
        '<input type="hidden" name="member_id" value="' + member_id + '">'
        + carry(inject)
        + '<input type="submit" value="Open Sub-Account" '
        'style="font-family:Verdana;font-size:11px;padding:3px 14px;">'
        "&nbsp;&nbsp;"
        '<a href="' + url_for("search") + qs(inject) + '">'
        '<font face="Verdana" size="1">New Inquiry</font></a>'
        "</form>"
    )

    if inject == "popup":
        detail += maintenance_overlay()

    return page("Member Detail", detail)


# --------------------------------------------------------------------------------------
# Page 3 -- sub-account confirmation.
# --------------------------------------------------------------------------------------


def sub_account_number(member_id):
    """Deterministic so replay runs can assert on the confirmation without recording a nonce."""
    return "SUB-" + member_id + "-02"


@app.route("/confirm", methods=["GET", "POST"])
def confirm():
    inject = inject_of(request)
    early = preflight(inject)
    if early is not None:
        return early

    member_id = (request.values.get("member_id") or "").strip()
    record = MEMBERS.get(member_id)
    if record is None:
        return redirect(url_for("search") + qs(inject))

    return page(
        "Sub-Account Opened",
        '<table border="1" cellpadding="10" cellspacing="0" bordercolor="#808080" '
        'bgcolor="#ffffff" width="620"><tr><td>'
        '<font face="Verdana" size="2" color="#006600"><b>Sub-account opened successfully.'
        "</b></font>"
        '<div style="margin-top:10px;"><font face="Verdana" size="2">'
        "A new savings sub-account has been established for <b>" + record["name"]
        + "</b> (member " + member_id + ")."
        "</font></div>"
        '<table border="1" cellpadding="6" cellspacing="0" bordercolor="#a0a0a0" '
        'bgcolor="#fbfbf7" style="margin-top:12px;">'
        '<tr><td bgcolor="#eceae4"><font face="Verdana" size="2">New Sub-Account Number'
        "</font></td>"
        '<td><span style="font-family:Courier New,monospace;font-weight:bold;">'
        + sub_account_number(member_id)
        + "</span></td></tr>"
        '<tr><td bgcolor="#eceae4"><font face="Verdana" size="2">Status</font></td>'
        "<td><span>Active - posts next cycle</span></td></tr></table>"
        '<div style="margin-top:16px;"><a href="' + url_for("search") + qs(inject) + '">'
        '<font face="Verdana" size="2">Return to Member Inquiry</font></a></div>'
        "</td></tr></table>",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False)
