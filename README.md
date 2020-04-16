# Planning 

## Bugzilla to Github script

This script synchronizes Bugzilla bugs to Github using whiteboard labels.

The list of bugzilla issues being synchronized is controlled by the [Github labels](https://github.com/FirefoxGraphics/planning/labels) with the prefix BZ_ in this repository and corresponding whiteboard label without the BZ_ prefix in bugzilla.


For example:
When a github label named 'BZ_wr-android' exists, it will find all open bugzilla issues with the whiteboard label 'wr-android' and create/update the corresponding github issues.
When an issue is resolved on the bugzilla side, it will be closed on the github side.

This allows us to drag the issues into [github projects](https://github.com/orgs/FirefoxGraphics/projects) by filtering the label in the 'Add Cards' menu item like: `is:open label:BZ_desktop-zoom-nightly`.
It seems like Github remembers the last card query, making it easy to drag in cards that aren't on the board yet.

You can also setup the project to automatically move issues that are closed to the done column by setting up automation in that column. An example of that can be found on the [APZ project](https://github.com/orgs/FirefoxGraphics/projects/7).

This script is run every 15 minutes.

