# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/. */

# A script to mirror bugilla issues into github, where we can see
# them as part of our github-and-jira-based planning procedure.
#
# For every relevant bug we find in bugzilla, we create a corresponding
# github issue that:
#
#   * has matching summary and description text
#   * has the "bugzilla" label
#   * may have additional metadata in the issue description
#
# If such an issue already exists, we update it to match the bugzilla
# bug rather than creating a new one.
#
# Note that the mirroring is (for now) entirely one-way. Changes to bug summar,
# description of status in bugzilla will be pushed to github, but any changes
# in github will not be reflected back in bugzilla.

import re
import os
import urllib.parse

import requests
import json
from github import Github

DRY_RUN = False
CARDS_DRY_RUN = False
FORCE_CARDS_SYNC = True
VERBOSE_DEBUG = True

GH_USE_TEST_REPO = False
GH_REPO = 'FirefoxGraphics/planning'
GH_ORG = 'FirefoxGraphics'
GH_USER = None
GH_TEST_REPO = 'ktaeleman/planning-test'
GH_TEST_USER = 'ktaeleman'
GH_TEST_ORG = None

GH_OLD_REPOS = []
GH_LABEL = 'bugzilla'
GH_BZLABEL_PREFIX = 'BZ_'

BZ_URL = 'https://bugzilla.mozilla.org/rest'

SYNCED_ISSUE_TEXT = '\n\n---\n\N{LADY BEETLE} Issue is synchronized with Bugzilla [Bug {id}](https://bugzilla.mozilla.org/show_bug.cgi?id={id})\n'
SYNCED_ISSUE_BUGID_REGEX = re.compile(
    # N.B. we can't use a r'raw string' literal here because of the \N escape.
    '\N{LADY BEETLE} Issue is synchronized with Bugzilla \\[Bug (\\d+)\\]')
SEE_ALSO_ISSUE_REGEX_TEMPLATE = r'^https://github.com/{}/issues/\d+$'
SYNCED_ISSUE_CLOSE_COMMENT = 'Upstream bug has been closed with the following resolution: {resolution}.'

# Jira adds some metadata to issue descriptions, indicated by this separator.
# We want to preserve any lines like this from the github issue description.
JIRA_ISSUE_MARKER = '\N{BOX DRAWINGS LIGHT TRIPLE DASH VERTICAL}'

# For now, only look at recent bugs in order to preserve sanity.
# MIN_CREATION_TIME = '20200201'

config_data = {}

def log(msg, *args, **kwds):
    msg = str(msg)
    print(msg.format(*args, **kwds))


def get_json(url):
    """Fetch a URL and return the result parsed as JSON."""
    r = requests.get(url)
    r.raise_for_status()
    return r.json()

def translate_bmo_user_to_gh(bmo_mail):
  global config_data
  for user in config_data["bmo_to_bugzilla"]:
    if (user["bmo_mail"] == bmo_mail):
      return user["gh_user"]
  return ""

class BugSet(object):
    """A set of bugzilla bugs, which we might like to mirror into GitHub.

    This class knows how to query the bugzilla API to find bugs, and how to
    fetch appropriate metadata for mirroring into github.

    Importantly, it knows how to use a bugzilla API key to find confidential
    or security-sensitive bugs, and can report their existence without leaking
    potentially confidential details (e.g. by reporting a placeholder summary
    of "confidential issue" rather than the actual bug summary).

    Use `update_from_bugzilla()` to query for bugs and add them to an in-memory
    store, then access them as if this were a dict keyed by bug number.  Each bug
    will be a dict with the following fields:

        * `id`: The bug id, as an integer
        * `whiteboard`: The bug's whiteboard text, as a string
        * `is_open`: Whether the bug is open, as a boolean
        * `summary`: The one-line bug summry, as a string
        * `status`: The bug's status field, as a string
        * `comment0`: The bug's first comment, which is typically a longer description, as a string
        * `assignee`: The person this issue is currently assigned to
    """

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.bugs = {}

    def __getitem__(self, key):
        return self.bugs[str(key)]

    def __delitem__(self, key):
        del self.bugs[str(key)]

    def __iter__(self):
        return iter(self.bugs)

    def __len__(self):
        return len(self.bugs)

    def _create_bugzilla_url(self, **kwds):
        url = BZ_URL + '/bug?include_fields=id,is_open,see_also,whiteboard,depends_on'
        url += '&' + self._make_query_string(**kwds)
        if self.api_key is not None:
            url += '&api_key=' + self.api_key
        return url

    def get_from_bugzilla_auth(self, **kwds):
        found_bugs = set()
        if "blocking" in kwds and len(kwds["blocking"]) != 0:
            for blocking in kwds["blocking"]:
                dependencies = blocking["name"]
                while len(dependencies) != 0:
                    kwds["id"] = dependencies
                    url = self._create_bugzilla_url(**kwds)
                    print("Querying with dependency" + str(dependencies) + ": " + url)
                    dependencies = []
                    bugs_result = get_json(url)['bugs']
                    for bug in bugs_result:
                        bugid = str(bug['id'])
                        found_bugs.add(bugid)
                        
                        if bugid not in self.bugs:
                            self.bugs[bugid] = bug
                            self.bugs[bugid]["whiteboard"] += " " + blocking["name"]
                            dependencies.extend([str(i) for i in bug['depends_on']])
                        else:
                            self.bugs[bugid].update(bug)
                            if (blocking["name"] not in self.bugs[bugid]["whiteboard"]):
                                self.bugs[bugid]["whiteboard"] += " " + blocking["name"]

        else:
            url = self._create_bugzilla_url(**kwds)
            for bug in get_json(url)['bugs']:
                bugid = str(bug['id'])
                found_bugs.add(bugid)
                if bugid not in self.bugs:
                    self.bugs[bugid] = bug
                else:
                    self.bugs[bugid].update(bug)

        return found_bugs

    def update_from_bugzilla(self, **kwds):
        """Slurp in bugs from bugzilla that match the given query keywords."""
        # First, fetch a minimal set of "safe" metadata that we're happy to put in
        # a public github issue, even for confidential bugzilla bugs.
        # This is the only query that's allowed to use a BZ API token to access
        # confidential bug info.
        found_bugs = self.get_from_bugzilla_auth(**kwds)

        # Now make *unauthenticated* public API queries to fetch additional metadata
        # which we know is safe to make public. Any security-sensitive bugs will be
        # silently omitted from this query.
        if found_bugs:
            public_bugs = set()
            url = BZ_URL + '/bug?include_fields=id,is_open,see_also,summary,status,resolution,assigned_to'
            url += '&id=' + '&id='.join(found_bugs)
            for bug in get_json(url)['bugs']:
                bugid = str(bug['id'])
                public_bugs.add(bugid)
                self.bugs[bugid].update(bug)
            # Unlike with fetching bug metadata, trying to fetch comments for a confidential bug
            # will error the entire request rather than silently omitting it. So we have to filter
            # them out during the loop above. Note that the resulting URL is a bit weird, it's:
            #
            #   /bug/<bug1>/comment?ids=bug2,bug3...
            #
            # This allows us to fetch comments from multiple bugs in a single query.
            if public_bugs:
                url = BZ_URL + '/bug/' + public_bugs.pop() + '/comment'
                if public_bugs:
                    url += '?ids=' + '&ids='.join(public_bugs)
                for bugnum, bug in get_json(url)['bugs'].items():
                    bugid = str(bugnum)
                    self.bugs[bugid]['comment0'] = bug['comments'][0]['text']

    def _make_query_string(self, product=None, component=None, id=None, resolved=None,
                           creation_time=None, last_change_time=None, whiteboard=None, blocking=None):
        def listify(x): return x if isinstance(x, (list, tuple, set)) else (x,)

        def encode(x): return urllib.parse.quote(x, safe='')
        qs = []
        if product is not None:
            qs.extend('product=' + encode(p) for p in listify(product))
        if component is not None:
            qs.extend('component=' + encode(c) for c in listify(component))
        if id is not None:
            qs.extend('id=' + encode(i) for i in listify(id))
        if creation_time is not None:
            qs.append('creation_time=' + creation_time)
        if last_change_time is not None:
            qs.append('last_change_time=' + last_change_time)
        if whiteboard is not None:
            qs.append('status_whiteboard_type=anywords')
            qs.append('status_whiteboard=' + " ".join([label["name"] for label in whiteboard]))
        if resolved is not None:
            if resolved:
                raise ValueError(
                    "Sorry, I haven't looked up how to query for only resolved bugs...")
            else:
                qs.append('resolution=---')
        if len(qs) == 0:
            raise ValueError(
                "Cowardly refusing to query every bug in existence; please specify some filters")
        return '&'.join(qs)


class MirrorIssueSet(object):
    """A set of mirror issues from GitHub, which can be synced to bugzilla bugs.

    Given a `BugSet` containing the bugs that you want to appear in github, use
    like so:

        issues = MirrorIssueSet(GITHUB_TOKEN)
        issues.sync_from_bugset(bugs)

    This will ensure that every bug in the bugset has a corresponding mirror issue,
    creating or updating issues as appropriate. It will also close out any miror issues
    that do not appear in the bugset, on the assumption that they've been closed in
    bugzilla.
    """

    def __init__(self, repo, label, org = None, user = None, api_key=None):
        self._gh = Github(api_key)
        self._repo = self._gh.get_repo(repo)
        self._repo_name = repo
        self._labels = self._repo.get_labels()
        self._label = self._repo.get_label(label)

        if org is not None:
            self._org = self._gh.get_organization(org)
        elif user is not None:
            self._org = self._gh.get_user(user)

        self._projects = []
        projects = self._org.get_projects()
        for project in projects:
          project_info = {}
          project_info["project"] = project
          project_info["columns"] = []
          project_info["added_cards"] = []
          columns = project.get_columns()
          for column in columns:
            column_info = {}
            column_info["column"] = column
            column_info["cards"] = column.get_cards()
            project_info["columns"].append(column_info)
          self._projects.append(project_info)

        self._see_also_regex = re.compile(
            SEE_ALSO_ISSUE_REGEX_TEMPLATE.format(repo))
        # The mirror issues, indexes by bugzilla bugid.
        self.mirror_issues = {}

    def get_bugzilla_sync_labels(self):
        """Get all the BZ_ bugzilla whiteboard labels defined in Github that this repository wants to sync"""
        bz_labels = list(filter(lambda label: label.name.startswith(GH_BZLABEL_PREFIX), self._labels))
        bz_bugid_regex = r"BZ_[0-9]+"
        return [{ "name": label.name[len(GH_BZLABEL_PREFIX):], 
                  "project": label.description, 
                  "type": "bugid" if re.match(bz_bugid_regex, label.name) else "whiteboard"
                } for label in bz_labels]

    def sync_from_bugset(self, bugs, updates_only=False):
        """Sync the mirrored issues with the given BugSet (which might be modified in-place)."""
        self.update_from_github()
        log('Found {} mirror issues in github', len(self.mirror_issues))
        # Fetch details for any mirror issues that are not in the set.
        # They might be e.g. closed, or have been moved to a different component,
        # but we still want to update them in github.
        missing_bugs = [
            bugid for bugid in self.mirror_issues if bugid not in bugs]
        if missing_bugs:
            log('Fetching info for {} missing bugs from bugzilla', len(missing_bugs))
            bugs.update_from_bugzilla(id=missing_bugs)
        num_updated = 0
        for bugid in bugs:
            if updates_only and bugid not in self.mirror_issues:
                if VERBOSE_DEBUG:
                    log('Not creating new bug {} in old repo', bugid)
                continue
            if self.sync_issue_from_bug_info(bugid, bugs[bugid]):
                num_updated += 1
        if num_updated > 0:
            log('Updated {} issues from bugzilla to github', num_updated)
        else:
            log('Looks like everything is up-to-date in {}', self._repo_name)

    def update_from_github(self):
        """Find mirror issues in the github repo.

        We assume they have a special label for easy searching, and some text in the issue
        description that tells us what bug it links to.
        """
        for issue in self._repo.get_issues(state='open', labels=[self._label]):
            match = SYNCED_ISSUE_BUGID_REGEX.search(issue.body)
            if not match:
                log("WARNING: Mirror issue #{} does not have a bugzilla bugid", issue.number)
                continue
            bugid = match.group(1)
            if bugid in self.mirror_issues:
                log("WARNING: Duplicate mirror issue #{} for Bug {}",
                    issue.number, bugid)
                continue
            self.mirror_issues[bugid] = issue

    def get_project_from_label(self, label):
        for project in self._projects:
          if "[project=" + str(project["project"].name).lower() + "]" in label.description.lower():
            return project
        return None

    def get_card_from_issue(self, project, issue):
        for column in project["columns"]:
          for card in column["cards"]:
            if card.content_url == issue.url:
              return column, card
        return None, None

    def get_column_for_issue(self, project, issue, is_assigned):
        # Determine in which column the issue should be
        if issue.state == 'open':
          if is_assigned:
            targetcolumns = [column for column in project["columns"] if column["column"].name.lower() == "in progress"]
            return None if len(targetcolumns) == 0 else targetcolumns[0]
          else:
            targetcolumns = [column for column in project["columns"] if column["column"].name.lower() == "not started" or column["column"].name.lower() == "to do"]
            return None if len(targetcolumns) == 0 else targetcolumns[0]
        else:
          targetcolumns = [column for column in project["columns"] if column["column"].name.lower() == "done"]
          return None if len(targetcolumns) == 0 else targetcolumns[0]

    def update_cards_for_issue(self, issue, is_assigned):
        for label in issue.get_labels():
          project = self.get_project_from_label(label)
          if project and issue.number not in project["added_cards"]:
            current_column, card = self.get_card_from_issue(project, issue)
            target_column = self.get_column_for_issue(project, issue, is_assigned)

            # Check if a card needs to be created or moved
            if not card and target_column:
              if VERBOSE_DEBUG:
                log('Creating card for issue #{} in {} - {}', issue.number, project['project'].name, target_column['column'].name)
              if not CARDS_DRY_RUN:
                card = target_column["column"].create_card(content_type = "Issue", content_id = issue.id)
                project["added_cards"].append(issue.number)
            elif target_column and current_column != target_column:
              is_custom_column = not current_column['column'].name.lower() in ['not started', 'to do', 'in progress', 'done']
              # When the issue is in a custom named column, only allow moves to in progress and done. This could be a sprint planning column.
              if not is_custom_column or not target_column['column'].name.lower() in ['not started']:
                if VERBOSE_DEBUG:
                  log('Moving card for issue #{} in {} - {}', issue.number, project['project'].name, target_column['column'].name)
                if not CARDS_DRY_RUN:
                  # card.move api does not work, delete/create for now.
                  # Should try move in the future: card.move("bottom", target_column["column"].id)
                  card.delete()
                  card = target_column['column'].create_card(content_type = "Issue", content_id = issue.id)
              elif VERBOSE_DEBUG:
                  log('Not moving card due to custom column for issue #{} in {} - {}', issue.number, project['project'].name, current_column['column'].name)

    def compare_issues(self, issue_info, issue):
        changed_fields = []
        for field in issue_info:
          if field == 'assignee':
            old = issue.assignee.login if issue.assignee else ""
            new = issue_info.get('assignee', "")
            if (old != new):
              changed_fields.append(field)
          elif issue_info[field] != getattr(issue, field):
            changed_fields.append(field)
        return changed_fields

    def sync_issue_from_bug_info(self, bugid, bug_info):
        issue = self.mirror_issues.get(bugid, None)
        issue_info = self._format_issue_info(bug_info, issue)
        is_assigned = bug_info.get('assigned_to', "") != "nobody@mozilla.org"
        if issue is None:
            if bug_info['is_open']:
                # As a light hack, if the bugzilla bug has a "see also" link to an issue in our repo,
                # we assume that's an existing mirror issue and avoid creating a new one. This lets us
                # keep the bug open in bugzilla but close it in github without constantly creating new
                # mirror issues.
                for see_also in bug_info.get('see_also', ()):
                    if self._see_also_regex.match(see_also) is not None:
                        log('Ignoring bz{id}, which links to {} via see-also',
                            see_also, **bug_info)
                        break
                else:
                    issue_info.pop('state')
                    log('Creating mirror issue for bz{id}', **bug_info)
                    if DRY_RUN:
                        issue = {}
                    else:
                        issue = self._repo.create_issue(**issue_info)
                        self.update_cards_for_issue(issue, is_assigned)
                    self.mirror_issues[bugid] = issue
                    return True
        else:
            changed_fields = self.compare_issues(issue_info, issue)
            if changed_fields:
                # Note that this will close issues that have not open in bugzilla.
                log('Updating mirror issue #{} for bz{id} (changed: {})',
                    issue.number, changed_fields, **bug_info)
                # Weird API thing where `issue.edit` accepts strings rather than label refs...
                issue_info['labels'] = [l.name for l in issue_info['labels']]
                # Explain why we are closing this issue.
                if not DRY_RUN:
                    if not bug_info['is_open'] and 'state' in changed_fields and 'resolution' in bug_info:
                        issue.create_comment(SYNCED_ISSUE_CLOSE_COMMENT.format(resolution=bug_info['resolution']))
                    issue.edit(**issue_info)
                    self.update_cards_for_issue(issue, is_assigned)
                return True
            else:
                if FORCE_CARDS_SYNC:
                  self.update_cards_for_issue(issue, is_assigned)
                if VERBOSE_DEBUG:
                    log('No change for issue #{}', issue.number)
        return False

    def _format_issue_info(self, bug_info, issue):
        issue_info = {
            'state': 'open' if bug_info['is_open'] else 'closed'
        }
        if 'summary' in bug_info:
            issue_info['title'] = bug_info['summary']
        else:
            issue_info['title'] = 'Confidential Bugzilla issue'
        if 'comment0' in bug_info:
            issue_info['body'] = bug_info['comment0']
        else:
            issue_info['body'] = 'No description is available for this confidential bugzilla issue.'

        # only change assignee if the issue is still open
        gh_user = translate_bmo_user_to_gh(bug_info['assigned_to'])
        if gh_user != "" and issue_info['state'] == 'open':
          issue_info['assignee'] = gh_user if not GH_USE_TEST_REPO else GH_TEST_USER

        if issue is None:
            issue_info['labels'] = [self._label]
        else:
            issue_info['labels'] = issue.labels
            if self._label not in issue.labels:
                issue_info['labels'].append(self._label)

        # if we have a whiteboard tag, make sure there is a corresponding Github label and remove labels that are no longer present
        valid_labels = []
        for label in issue_info['labels']:
          if not label.name.startswith(GH_BZLABEL_PREFIX):
            valid_labels.append(label)

        for label in re.split(r'[\s,;]+', bug_info['whiteboard']):
          # Remove special characters from label string so the bugzilla whiteboard syntax can be used (eg. [gfx-noted] [fenix:p1])
          sanitized_label = label.translate({ord(c): None for c in '[:]'})
          gh_label_name = "BZ_" + sanitized_label.strip()
          # only add BZ_ whiteboard label if it exists in the github labels
          gh_label = next((x for x in self._labels if x.name == gh_label_name), None)
          if gh_label:
            valid_labels.append(gh_label)

        issue_info['labels'] = sorted(valid_labels, key=lambda label: label.name)

        # Ensure we include a link to the bugzilla bug for reference.
        issue_info['body'] += SYNCED_ISSUE_TEXT.format(**bug_info)

        # Preserve any Jira sync lines in the issue body.
        if issue is not None:
            for ln in issue.body.split("\n"):
                if ln.startswith(JIRA_ISSUE_MARKER):
                    issue_info['body'] += '\n' + ln
            # Jira seems to sometimes add a trailing newline, try to match it to avoid spurious updates.
            if issue.body.endswith('\n') and not issue_info['body'].endswith('\n'):
                issue_info['body'] += '\n'

        return issue_info


def sync_bugzilla_to_github():
    global config_data
    # Find the sets of bugs in bugzilla that we want to mirror.
    gh_token = os.environ.get('GITHUB_TOKEN')

    # Load configuration
    with open('config.json') as f:
      config_data = json.load(f)


    log('Finding relevant bugs in bugzilla...')
    bugs = BugSet(os.environ.get('BZ_API_KEY'))
    issues = None
    if GH_USE_TEST_REPO:
        issues = MirrorIssueSet(GH_TEST_REPO, GH_LABEL, org=GH_TEST_ORG, user=GH_TEST_USER, api_key=gh_token)
    else:
        issues = MirrorIssueSet(GH_REPO, GH_LABEL, org=GH_ORG, user=GH_USER, api_key=gh_token)

    labels = issues.get_bugzilla_sync_labels()
    whiteboard_labels = list(filter(lambda label: label['type'] == "whiteboard", labels))
    blocking_labels = list(filter(lambda label: label['type'] == "bugid", labels))

    bugs.update_from_bugzilla(product=['Core','Firefox','GeckoView'],
                              resolved=False, 
                              whiteboard=whiteboard_labels
                             )
    
    bugs.update_from_bugzilla(product=['Core','Firefox','GeckoView'],
                              resolved=False, 
                              blocking=blocking_labels
                             )
    
    # bugs.update_from_bugzilla(product='Firefox', component='Sync',
    #                           resolved=False, creation_time=MIN_CREATION_TIME)
    log('Found {} bugzilla bugs', len(bugs))

    # Find any that are already represented in old github repos.
    # We don't want to make duplicates of them in the current repo!
    for old_repo in GH_OLD_REPOS:
        log('Syncing to old github repo at {}', old_repo)
        old_issues = MirrorIssueSet(old_repo, GH_LABEL, gh_token)
        old_issues.sync_from_bugset(bugs, updates_only=True)
        done_count = 0
        for bugid in old_issues.mirror_issues:
            if bugid in bugs:
                del bugs[bugid]
                done_count += 1
        log('Synced {} bugs, now {} left to sync', done_count, len(bugs))

    log('Syncing to github repo at {}', GH_REPO)

    issues.sync_from_bugset(bugs)
    log('Done!')


if __name__ == "__main__":
    sync_bugzilla_to_github()
