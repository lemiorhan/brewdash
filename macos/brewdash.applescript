-- brewdash.app. Compiled by macos/build-app.sh as a stay-open applet: it stays in the Dock while the
-- dashboard server runs, a click on the Dock icon shows the dashboard again, and quitting stops the server.
-- No properties or globals: an applet writes them back into its own bundle, which breaks its signature.

on run
	try
		set dashboardUrl to do shell script launcher("open " & (do shell script "echo $PPID"))
	on error errorMessage
		display alert "brewdash could not start" message errorMessage as critical
		quit
		return
	end try
	open location dashboardUrl
end run

on reopen
	try
		set dashboardUrl to do shell script launcher("open " & (do shell script "echo $PPID"))
	on error errorMessage
		display alert "brewdash could not start" message errorMessage as critical
		return
	end try
	open location dashboardUrl
end reopen

on quit
	try
		do shell script launcher("stop")
	end try
	continue quit
end quit

on launcher(command)
	return "/bin/bash " & quoted form of (POSIX path of (path to me) & "Contents/Resources/brewdash/launch.sh") & " " & command
end launcher
