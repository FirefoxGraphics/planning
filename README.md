# Planning

## Bugzilla to Github script

This script synchronizes Bugzilla bugs to Github using whiteboard labels. It is based on the bmo->gh sync script from Mozilla Application Services. The original script can be found here: https://github.com/mozilla/application-services/tree/master/tools.

This script is run every 15 minutes.

### Synchronizing bugs

The list of bugzilla issues being synchronized is controlled by the [Github labels](https://github.com/FirefoxGraphics/planning/labels) with the prefix BZ_ in this repository.
The labels can sync whiteboard tags from bugzilla and/or bug dependencies.

For example:
When a github label named 'BZ_wr-android' exists, it will find all open bugzilla issues with the whiteboard label 'wr-android' and create/update the corresponding github issues.
When a github label named 'BZ_1624521' is encountered, it will sync all dependencies of that issues and apply the label to them.
When an issue is resolved on the bugzilla side, it will be closed on the github side.

This allows us to drag the issues into [github projects](https://github.com/orgs/FirefoxGraphics/projects) by filtering the label in the 'Add Cards' menu item like: `is:open label:BZ_desktop-zoom-nightly`.
It seems like Github remembers the last card query, making it easy to drag in cards that aren't on the board yet.
If you set up Automatic project updates (see below), cards will be created automatically.

If users are recognized and have a Github counterpart (hardcoded for now), issues will also get assigned in Github.

### Automatic project updates

If you want to use automatic project updates for your issues, you will need to tell the script which labels need to be added to which projects.
In the Github label description, add the following tag: \[project=PROJECTNAME\].

This will:
* Create cards for new issues in the "to do" or "not started" column
* Move issues assigned in bugzilla to the "in progress" column
* Move closed issues to the "done" column

The column names are case insensitive. If an issue is in a column that's not in the above list, the issue will not be moved to the "to do" or "not started" column, but will move to "in progress" or "done". This is done so custom planning columns can be used, for example for planning sprints.




