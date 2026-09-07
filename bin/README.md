Publisher downloads are cached under the helper's data directory, not committed
to Git. The existing `Install_NGM.exe_USEIFNEEDED` is retained as a manual fallback.
The automated helper bootstraps NGM from Nexon's current component manifest and
tracks the publisher's component revisions, so it does not depend on that installer snapshot.
