-- Shows a page of a PDF open in Preview. Run by opener.py after `open -a Preview <file>`:
--     osascript preview_goto.applescript LABEL PAGE NAME
-- LABEL is what to enter in Go > Go to Page… (Preview reads it as the PDF's printed page
-- label), PAGE the physical page wanted (1 = first page of the file), NAME the file name.
--
-- Preview has no scripting command for this, so it is driven through System Events, which
-- needs the Accessibility permission. Menu items are found by their keyboard shortcut, not
-- their localized name, and clicked rather than typed: another app may own a shortcut
-- globally (Google Drive took ⌥⌘G). Nothing is typed: the label goes straight into the
-- sheet's text field, and if the sheet doesn't appear the script stops.

on run argv
	set pageLabel to item 1 of argv
	set target to (item 2 of argv) as integer
	set docName to item 3 of argv
	tell application "System Events"
		tell process "Preview"
			-- big PDFs take a moment: wait until this document's window is in front
			repeat 80 times
				if (count of windows) > 0 then
					if name of window 1 contains docName then exit repeat
				end if
				delay 0.1
			end repeat
			if name of window 1 does not contain docName then error "The PDF window was not found" number 9001
			set frontmost to true
			click my menuItemWith("AXMenuItemCmdChar", "G", 2) -- Go to Page… (⌥⌘G)
			repeat 30 times
				if (count of sheets of window 1) > 0 then exit repeat
				delay 0.1
			end repeat
			if (count of sheets of window 1) = 0 then error "Go to Page did not appear" number 9002
			set dlg to sheet 1 of window 1
			set value of text field 1 of dlg to pageLabel
			click (value of attribute "AXDefaultButton" of dlg)

			-- The title says which physical page is shown ("Book.pdf – Page 265 of 553").
			-- Page Down/Up until it is the wanted one: needed when the label repeats an earlier
			-- page's or is empty, and a check otherwise.
			set goingDown to missing value
			repeat 40 times
				delay 0.15
				set current to my shownPage(name of window 1)
				if current is missing value or current = target then exit repeat
				if goingDown is missing value then set goingDown to (current < target)
				if goingDown and current > target then exit repeat -- passed it: close enough
				if (not goingDown) and current < target then exit repeat
				if goingDown then
					click my menuItemWith("AXMenuItemCmdVirtualKey", 121, 8) -- Page Down
				else
					click my menuItemWith("AXMenuItemCmdVirtualKey", 116, 8) -- Page Up
				end if
			end repeat
		end tell
	end tell
end run

-- The menu item whose shortcut has attribute attr = val and modifier flags mods.
on menuItemWith(attr, val, mods)
	tell application "System Events"
		tell process "Preview"
			set n to count of menu bar items of menu bar 1
			set order to {6} -- the Go menu in current versions; then the rest
			repeat with i from 2 to n
				if i is not 6 then set end of order to i
			end repeat
			repeat with i in order
				set i to contents of i
				if i ≤ n then
					try
						set m to menu 1 of menu bar item i of menu bar 1
						set vals to value of attribute attr of menu items of m
						set modList to value of attribute "AXMenuItemCmdModifiers" of menu items of m
						repeat with j from 1 to count of vals
							if item j of vals = val and item j of modList = mods then return menu item j of m
						end repeat
					end try
				end if
			end repeat
		end tell
	end tell
	error "Preview menu item not found" number 9003
end menuItemWith

-- "Book.pdf – Page 265 of 553" -> 265, in any language (missing value if the title has no page).
on shownPage(title)
	set AppleScript's text item delimiters to " – "
	set parts to text items of title
	set AppleScript's text item delimiters to ""
	if (count of parts) < 2 then return missing value
	set num to ""
	repeat with c in characters of (last item of parts)
		set c to contents of c
		if c is in "0123456789" then
			set num to num & c
		else if num is not "" then
			exit repeat
		end if
	end repeat
	if num is "" then return missing value
	return num as integer
end shownPage
