Scriptname GoslingOrders Hidden
{Orders to Gosling by voice: wait / follow / dismiss. Called by the SkyrimCompanion SKSE plugin.
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
	Else
		Done(asId, "unknown_action")
		Return
	EndIf
	akGosling.EvaluatePackage()
	Done(asId, "")
EndFunction

; Go to a place (a map marker reference) and then wait there (abStay) or come back to the player.
; PathToReference is latent: returns once he got there (true) or the game gave up on the path (false).
; Pathing and fights on the way are the game's AI; while walking he stays the player's follower.
; Only within the loaded area: to a target ~580 m away it returned TRUE after 27 s with him moved 1 m (2026-10-21) -
; so a far target is refused, and "reached" is checked by the distance left, not trusted.
Function GoTo(Actor akGosling, ObjectReference akTarget, bool abStay, string asId) global
	float maxUnits = 10000.0     ; ~140 m (70 units per meter) - about the loaded area around the player
	float arrivedUnits = 1400.0  ; ~20 m: a map marker may stand off the navmesh
	DialogueFollowerScript follower = Game.GetFormFromFile(0x750BA, "Skyrim.esm") as DialogueFollowerScript
	If !akGosling || !akTarget || !follower
		Done(asId, "no_quest")
		Return
	EndIf
	If follower.pFollowerAlias.GetActorRef() != akGosling
		Done(asId, "not_follower")
		Return
	EndIf
	float distance = akGosling.GetDistance(akTarget)
	GoslingVoice.Log("GoTo " + asId + ": start, distance " + distance + ", 3D loaded: " + akTarget.Is3DLoaded())
	If distance > maxUnits
		Done(asId, "too_far")
		Return
	EndIf
	bool reached = akGosling.PathToReference(akTarget, 1.0)
	float left = akGosling.GetDistance(akTarget)
	GoslingVoice.Log("GoTo " + asId + ": reached " + reached + ", distance left " + left)
	If !reached || left > arrivedUnits
		Done(asId, "no_path")
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
