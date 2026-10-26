Scriptname GoslingOrders Hidden
{Orders to Gosling by voice: wait / follow / dismiss / go to a place. Called by the SkyrimCompanion SKSE plugin.
Goes through the vanilla follower quest DialogueFollower - exactly what the "Wait here" / "Follow me" /
"I need to part ways" dialogue does, so the game's own follower logic (wait packages, 3-day wait timer) stays in charge.}

; Implemented in the SKSE plugin: the result goes back to the companion service (event order_result).
; asReason: "" - done; otherwise a code the service turns into words.
Function Done(string asId, string asReason) global native

Function Order(Actor akGosling, string asAction, string asId) global
	DialogueFollowerScript follower = Game.GetFormFromFile(0x750BA, "Skyrim.esm") as DialogueFollowerScript
	If !akGosling || !follower
		Done(asId, "no_quest")
		Return
	EndIf
	If akGosling.IsDead()
		Done(asId, "dead")
		Return
	EndIf
	StopTravel()  ; any new order cancels a walk to a place (GoTo then reports "cancelled")
	Actor current = follower.pFollowerAlias.GetActorRef()
	If asAction == "follow"
		If current == akGosling
			follower.FollowerFollow()
		ElseIf current
			; The player already has another follower - the vanilla dialogue would not offer "Follow me" either.
			Done(asId, "other_follower")
			Return
		Else
			follower.SetFollower(akGosling)
		EndIf
	ElseIf asAction == "wait"
		If current != akGosling
			Done(asId, "not_follower")
			Return
		EndIf
		follower.FollowerWait()
	ElseIf asAction == "dismiss"
		If current != akGosling
			Done(asId, "not_follower")
			Return
		EndIf
		; Standard message, no farewell line: that would be Erik's recorded voice - Gosling says goodbye himself.
		; Then his own AI package takes over: he walks back to his place in the inn.
		follower.DismissFollower(0, 0)
	ElseIf asAction == "release"
		; Stop keeping close to a person (Approach) - stay here, as after any walk of the explorer.
		If current != akGosling
			Done(asId, "not_follower")
			Return
		EndIf
		follower.FollowerWait()
	ElseIf asAction == "linger"
		If current != akGosling
			Done(asId, "not_follower")
			Return
		EndIf
		follower.FollowerWait()  ; released ("release" or any order) - he stays here
		Linger(akGosling, asId)  ; reports itself
		Return
	ElseIf asAction == "unstick"
		If current != akGosling
			Done(asId, "not_follower")
			Return
		EndIf
		follower.FollowerFollow()
		akGosling.EvaluatePackage()
		Unstick(akGosling, asId)  ; reports itself
		Return
	Else
		Done(asId, "unknown_action")
		Return
	EndIf
	akGosling.EvaluatePackage()
	Done(asId, "")
EndFunction

; The walk quest of Gosling.esp (0x815): alias 0 "Traveler" carries the travel package (to alias 1 "Destination").
Quest Function TravelQuest() global
	Return Game.GetFormFromFile(0x815, "Gosling.esp") as Quest
EndFunction

Function StopTravel() global
	Quest travel = TravelQuest()
	If travel && travel.IsRunning()
		ClearSpot(travel)
		travel.Stop()  ; clears the aliases: the travel package is gone, following takes over again
	EndIf
EndFunction

; Off every package of the walk quest before a new target: the same package on the same alias, only Destination changed -
; EvaluatePackage keeps it running with the old target (2026-10-24: done talking to Bergitta, sent to Lilith - he went on
; following Bergitta, "no_path" with the distance to Lilith growing 20 -> 28 m). Off, evaluated - the new one starts afresh.
Function DropPackages(Actor akGosling, Quest akTravel) global
	ClearSpot(akTravel)
	(akTravel.GetAlias(0) as ReferenceAlias).Clear()  ; Traveler
	(akTravel.GetAlias(2) as ReferenceAlias).Clear()  ; Leader
	(akTravel.GetAlias(3) as ReferenceAlias).Clear()  ; Approacher
	(akTravel.GetAlias(4) as ReferenceAlias).Clear()  ; Lingerer
	(akTravel.GetAlias(1) as ReferenceAlias).Clear()  ; Destination
	akGosling.EvaluatePackage()
EndFunction

; The mark Linger placed where he stood (alias Destination) - delete it before Destination gets another target.
Function ClearSpot(Quest akTravel) global
	ObjectReference spot = (akTravel.GetAlias(1) as ReferenceAlias).GetReference()
	If spot && spot.GetBaseObject() == Game.GetFormFromFile(0x3B, "Skyrim.esm")  ; XMarker
		spot.Delete()
	EndIf
EndFunction

; Stay here a while and listen (the explorer chose it; the player is near - the world around lives): alias Lingerer, its
; package is Sandbox around alias Destination - a mark placed where he stands. He stands, sits, leans on a wall like
; anyone in the tavern; how long - the service decides and then sends the next order ("release" among them).
Function Linger(Actor akGosling, string asId) global
	Quest travel = TravelQuest()
	If !travel || (!travel.IsRunning() && !travel.Start())
		Done(asId, "no_quest")
		Return
	EndIf
	ObjectReference spot = akGosling.PlaceAtMe(Game.GetFormFromFile(0x3B, "Skyrim.esm"))  ; XMarker
	(travel.GetAlias(1) as ReferenceAlias).ForceRefTo(spot)
	(travel.GetAlias(4) as ReferenceAlias).ForceRefTo(akGosling)
	akGosling.EvaluatePackage()
	GoslingVoice.Log("Linger " + asId + ": here")
	Done(asId, "")
EndFunction

; Go to a place (a map marker reference), then wait there (abStay) or come back to the player.
; The walk is an AI package, "travel to alias Destination", on the alias Traveler of the walk quest: its priority is
; above the follower quest, so it overrides following; the game carries a package on outside the loaded area too.
; (PathToReference does not: to a target ~580 m away it returned TRUE after 27 s with him moved 1 m, 2026-10-21.)
; abLead - "lead the way": alias Leader instead, its package escorts the player to Destination and waits when the
; player falls behind (the world around him is then simulated in full); the player sets the pace - more time.
; Arrival is checked by the distance left every second. Stuck - he walks to the player (the player is his landmark: he
; stands where paths are) and, out of that spot, on to the place from there; a few times, then give up.
; afArrive - arrived when closer than this (units); 0 - ~20 m (a map marker may stand off the navmesh); to a person - closer.
Function GoTo(Actor akGosling, ObjectReference akTarget, bool abStay, bool abLead, string asId, float afArrive = 0.0) global
	float arrivedUnits = 1400.0  ; ~20 m (70 units per meter): a map marker may stand off the navmesh
	If afArrive > 0.0
		arrivedUnits = afArrive
	EndIf
	float slowUnits = 140.0      ; per second - well below his run: allowed time = distance / slowUnits + 60 s
	If abLead
		slowUnits = 50.0         ; the player's pace, with stops to look around
	EndIf
	DialogueFollowerScript follower = Game.GetFormFromFile(0x750BA, "Skyrim.esm") as DialogueFollowerScript
	Quest travel = TravelQuest()
	If !akGosling || !akTarget || !follower || !travel
		Done(asId, "no_quest")
		Return
	EndIf
	If follower.pFollowerAlias.GetActorRef() != akGosling
		Done(asId, "not_follower")
		Return
	EndIf
	If akTarget.IsDisabled()
		; The place is not in the world yet (a HearthFire plot before the land is bought): no path to it at all.
		GoslingVoice.Log("GoTo " + asId + ": target disabled")
		Done(asId, "no_path")
		Return
	EndIf
	If !travel.IsRunning() && !travel.Start()
		Done(asId, "no_quest")
		Return
	EndIf
	DropPackages(akGosling, travel)
	Actor person = akTarget as Actor
	If person
		Approach(akGosling, person, asId, afArrive)  ; reports itself
		Return
	EndIf
	ReferenceAlias traveler = travel.GetAlias(0) as ReferenceAlias
	ReferenceAlias destination = travel.GetAlias(1) as ReferenceAlias
	If abLead
		traveler = travel.GetAlias(2) as ReferenceAlias  ; Leader: one package at a time - walking alone or leading
	EndIf
	destination.ForceRefTo(akTarget)
	traveler.ForceRefTo(akGosling)
	akGosling.EvaluatePackage()
	float distance = akGosling.GetDistance(akTarget)
	int ticks = 0  ; seconds
	If distance > 1000000000.0
		; The target is in another cell (inside a building, 2026-10-24): GetDistance is "infinite" until he is in there -
		; the allowance was negative and every walk inside ended at once with no_path. A fixed allowance then.
		ticks = 300
		If abLead
			ticks = 600
		EndIf
	Else
		ticks = (distance / slowUnits + 60.0) as int
	EndIf
	GoslingVoice.Log("GoTo " + asId + ": start, distance " + distance + ", allowed " + ticks + " s, lead " + abLead)
	float left = distance
	Actor player = Game.GetPlayer()
	float anchorX = akGosling.GetPositionX()
	float anchorY = akGosling.GetPositionY()
	float stuckX = 0.0
	float stuckY = 0.0
	int stuck = 0
	int detours = 0         ; how many times he went to the player to get out
	bool toPlayer = false   ; now: walking to the player (traveler alias empty - following leads him)
	int toPlayerFor = 0     ; s
	int grace = 8           ; s - the start of a walk: the game is still finding the path, he stands - not stuck yet
	; Game time stops in menus - so does Utility.Wait: the allowance counts only time in the world.
	; Close targets (a person, a door): the travel package itself stops ~7 m short (its radius, 500 units) - so the walk
	; ends at the package radius and the last steps are PathToReference (2026-10-24: asked for 5 m, he stood at 7 m,
	; "stuck" three times - no_path; to people and to Belethor's door).
	float packageNear = 700.0
	While left > arrivedUnits && !(arrivedUnits < packageNear && left <= packageNear) && ticks > 0 && destination.GetReference() == akTarget && ((toPlayer && !traveler.GetReference()) || (!toPlayer && traveler.GetReference() == akGosling))
		Utility.Wait(1.0)
		left = akGosling.GetDistance(akTarget)
		ticks -= 1
		; Stuck: ~5 s he has not left a ~4 m circle though nothing holds him (alone, or leading with the player close; not
		; in a fight). His own steps, not the distance left: going round a lake or a rock, the distance grows for a while;
		; a circle, not each step: walking into rocks he slides back and forth (2026-10-24). Unloaded he is moved in
		; jumps - not checked then.
		float dx = akGosling.GetPositionX() - anchorX
		float dy = akGosling.GetPositionY() - anchorY
		If grace > 0
			grace -= 1
		EndIf
		If grace > 0 || !akGosling.Is3DLoaded() || akGosling.IsInCombat() || dx * dx + dy * dy > 90000.0 || (abLead && !toPlayer && player.GetDistance(akGosling) >= 1400.0)
			stuck = 0
			anchorX = akGosling.GetPositionX()
			anchorY = akGosling.GetPositionY()
		Else
			stuck += 1
		EndIf
		If toPlayer
			toPlayerFor += 1
			dx = akGosling.GetPositionX() - stuckX
			dy = akGosling.GetPositionY() - stuckY
			bool gotOut = dx * dx + dy * dy > 360000.0  ; ~8 m away from that spot
			; He is by the player already - following won't move him (2026-10-24, "lead": stood a minute next to the player):
			; on to the place from here, no moving him - standing next to the player he was moved before his eyes, often
			; (2026-10-24: in the crowd at the market he stands a while - "stuck"). Far from the player (~15 m) and out of
			; his view, ~10 s not walking to him (2026-10-21: not a step at all) - next to the player, the way the game
			; brings followers along.
			If !gotOut && player.GetDistance(akGosling) < 500.0
				gotOut = true
			ElseIf !gotOut && stuck >= 10 && player.GetDistance(akGosling) > 1000.0 && !player.HasLOS(akGosling)  ; player's HasLOS - in view
				float angle = player.GetAngleZ()
				akGosling.MoveTo(player, -150.0 * Math.Sin(angle), -150.0 * Math.Cos(angle))  ; ~2 m behind the player
				GoslingVoice.Log("GoTo " + asId + ": far and out of view - moved next to the player")
				gotOut = true
			EndIf
			If gotOut
				; Out of that spot - on to the place from here.
				traveler.ForceRefTo(akGosling)
				akGosling.EvaluatePackage()
				GoslingVoice.Log("GoTo " + asId + ": out - on to the place, distance left " + left)
				toPlayer = false
				stuck = 0
				grace = 5
				anchorX = akGosling.GetPositionX()
				anchorY = akGosling.GetPositionY()
			ElseIf toPlayerFor >= 60
				; A minute and not out - give up.
				GoslingVoice.Log("GoTo " + asId + ": can't get out, distance left " + left)
				ticks = 0
			EndIf
		ElseIf stuck >= 5
			If detours >= 3
				; Out and stuck again, three times - no path to the place from here.
				GoslingVoice.Log("GoTo " + asId + ": stuck, distance left " + left)
				ticks = 0
			Else
				; He can't get out of where he stands (walks into rocks, 2026-10-24): to the player - the player is his
				; landmark, he stands where paths are. Following leads him: the travel package off for now.
				detours += 1
				toPlayer = true
				toPlayerFor = 0
				stuck = 0
				stuckX = akGosling.GetPositionX()
				stuckY = akGosling.GetPositionY()
				traveler.Clear()
				follower.FollowerFollow()
				akGosling.EvaluatePackage()
				GoslingVoice.Log("GoTo " + asId + ": stuck - walking to the player (" + detours + "), distance left " + left)
			EndIf
		EndIf
	EndWhile
	If destination.GetReference() != akTarget || (!toPlayer && traveler.GetReference() != akGosling) || (toPlayer && traveler.GetReference())
		GoslingVoice.Log("GoTo " + asId + ": cancelled, distance left " + left)
		Done(asId, "cancelled")  ; a new order took over - it decides what he does now
		Return
	EndIf
	travel.Stop()
	If left > arrivedUnits && left <= packageNear && akGosling.Is3DLoaded() && akTarget.Is3DLoaded()
		; The last steps: inside the loaded area PathToReference works - right up to it (a person may walk on: it follows).
		akGosling.PathToReference(akTarget, 1.0)
		left = akGosling.GetDistance(akTarget)
		If left <= arrivedUnits + 210.0  ; ~3 m slack - a person moves while he walks up
			left = arrivedUnits
		EndIf
		GoslingVoice.Log("GoTo " + asId + ": last steps, distance left " + akGosling.GetDistance(akTarget))
	EndIf
	GoslingVoice.Log("GoTo " + asId + ": distance left " + left + ", 3D loaded: " + akGosling.Is3DLoaded())
	If left > arrivedUnits
		Done(asId, "no_path")  ; following takes over again: he comes back to the player by himself
		Return
	EndIf
	If abStay
		follower.FollowerWait()
	Else
		follower.FollowerFollow()
	EndIf
	akGosling.EvaluatePackage()
	Done(asId, "")
EndFunction

; Walk up to a person and keep close (alias Approacher, its package follows alias Destination a couple of steps behind):
; the person walks - he walks along, stops - he stands by. The travel package stopped ~7 m short and the last steps were
; one PathToReference: the person walked on meanwhile and it was "no path" (2026-10-24: Olfrid, 7 m -> 19 m).
; Arrived - closer than afArrive (units); he stays close after that (the talk), until the next order: StopTravel (any
; Order, "release" among them) or GoTo (clears Approacher). The person gone (into a house, out of the loaded area) - "gone".
Function Approach(Actor akGosling, Actor akPerson, string asId, float afArrive) global
	float arrivedUnits = 250.0  ; ~3.5 m
	If afArrive > 0.0
		arrivedUnits = afArrive
	EndIf
	Quest travel = TravelQuest()
	ReferenceAlias destination = travel.GetAlias(1) as ReferenceAlias
	ReferenceAlias approacher = travel.GetAlias(3) as ReferenceAlias
	destination.ForceRefTo(akPerson)
	approacher.ForceRefTo(akGosling)
	akGosling.EvaluatePackage()
	float left = akGosling.GetDistance(akPerson)
	; The person may walk away while he follows: time by the distance at the start, and plenty of it.
	int ticks = (left / 140.0 + 90.0) as int
	GoslingVoice.Log("Approach " + asId + ": start, distance " + left + ", allowed " + ticks + " s")
	float anchorX = akGosling.GetPositionX()
	float anchorY = akGosling.GetPositionY()
	int stuck = 0
	int grace = 5  ; s - the game is still finding the path
	string reason = ""
	While left > arrivedUnits && reason == ""
		If destination.GetReference() != akPerson || approacher.GetReference() != akGosling
			reason = "cancelled"  ; a new order took over - it decides what he does now
		ElseIf akPerson.IsDead() || akPerson.IsDisabled() || !akPerson.Is3DLoaded() || (akPerson.IsInInterior() && akPerson.GetParentCell() != akGosling.GetParentCell())
			reason = "gone"
		ElseIf ticks <= 0
			reason = "no_path"
		Else
			Utility.Wait(1.0)
			ticks -= 1
			left = akGosling.GetDistance(akPerson)
			; Stuck: ~10 s he has not left a ~4 m circle though nothing holds him - the game finds no path to the person.
			float dx = akGosling.GetPositionX() - anchorX
			float dy = akGosling.GetPositionY() - anchorY
			If grace > 0
				grace -= 1
			EndIf
			If grace > 0 || akGosling.IsInCombat() || dx * dx + dy * dy > 90000.0
				stuck = 0
				anchorX = akGosling.GetPositionX()
				anchorY = akGosling.GetPositionY()
			Else
				stuck += 1
				If stuck >= 10
					reason = "no_path"
				EndIf
			EndIf
		EndIf
	EndWhile
	GoslingVoice.Log("Approach " + asId + ": " + reason + ", distance left " + left)
	If reason == "cancelled"
		Done(asId, reason)
		Return
	EndIf
	If reason != ""
		approacher.Clear()  ; not keeping close to anyone: following the player again
		akGosling.EvaluatePackage()
		Done(asId, reason)
		Return
	EndIf
	Done(asId, "")
EndFunction

; He stands where the game finds no path from (walks to two places in a row from one spot - not a step): following,
; the player may lead him out. Still there after ~20 s and the player doesn't see him - next to the player, the way the
; game brings followers along. The player watches him all the time - give up after ~1 min (no_path).
Function Unstick(Actor akGosling, string asId) global
	Actor player = Game.GetPlayer()
	float startX = akGosling.GetPositionX()
	float startY = akGosling.GetPositionY()
	int ticks = 0
	While ticks < 30
		Utility.Wait(2.0)
		ticks += 1
		float dx = akGosling.GetPositionX() - startX
		float dy = akGosling.GetPositionY() - startY
		If dx * dx + dy * dy > 350.0 * 350.0  ; ~5 m away from that spot - out
			GoslingVoice.Log("Unstick " + asId + ": got out walking")
			Done(asId, "")
			Return
		EndIf
		If ticks >= 10 && !player.HasLOS(akGosling)  ; the player's HasLOS - is he in view
			float angle = player.GetAngleZ()
			akGosling.MoveTo(player, -150.0 * Math.Sin(angle), -150.0 * Math.Cos(angle))  ; ~2 m behind the player
			GoslingVoice.Log("Unstick " + asId + ": moved next to the player")
			Done(asId, "")
			Return
		EndIf
	EndWhile
	GoslingVoice.Log("Unstick " + asId + ": still stuck")
	Done(asId, "no_path")
EndFunction

; Take a loose item or loot a container near him (the explorer chose it; it is not owned - no stealing). Walk up to it
; first: inside the loaded area PathToReference works. Quest items stay: a container keeps them (RemoveAllItems without
; quest items), a loose quest item is never chosen - the player takes those himself. Locked - no: he can't pick locks.
Function Take(Actor akGosling, ObjectReference akTarget, string asId) global
	If !akGosling || !akTarget
		Done(asId, "no_target")
		Return
	EndIf
	akGosling.PathToReference(akTarget, 0.5)
	If akGosling.GetDistance(akTarget) > 350.0  ; ~5 m
		Done(asId, "no_path")
		Return
	EndIf
	If akTarget.GetBaseObject() as Container
		If akTarget.IsLocked()
			Done(asId, "locked")
			Return
		EndIf
		akTarget.RemoveAllItems(akGosling, false, false)
	Else
		akGosling.AddItem(akTarget, 1, true)
	EndIf
	GoslingVoice.Log("Take " + asId + ": " + akTarget)
	Done(asId, "")
EndFunction

; Hand over to the player what he found (the explorer's finds): the player sees the usual "added" notifications.
Function Give(Actor akGosling, Form akItem, int aiCount, string asId) global
	If !akGosling || !akItem
		Done(asId, "no_target")
		Return
	EndIf
	akGosling.RemoveItem(akItem, aiCount, false, Game.GetPlayer())
	Done(asId, "")
EndFunction

; Gosling found a place the player has not discovered: put its map marker on the player's map (seen, no fast travel -
; the player has not been there himself).
Function MarkOnMap(ObjectReference akMarker, string asId) global
	If !akMarker
		Done(asId, "no_target")
		Return
	EndIf
	akMarker.AddToMap(false)
	GoslingVoice.Log("MarkOnMap " + asId + ": " + akMarker)
	Done(asId, "")
EndFunction
