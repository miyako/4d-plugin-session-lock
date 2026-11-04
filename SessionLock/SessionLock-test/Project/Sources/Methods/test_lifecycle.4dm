//%attributes = {"invisible":true}
var $initial; $state; $first; $again; $stop; $batch; $restart : Object

// State and Events must never implicitly start monitoring.
$initial:=SessionLock State
assert_state($initial)
assert_stopped($initial)
ASSERT($initial.generation="0")
ASSERT($initial.sequence="0")
ASSERT($initial.droppedTotal="0")
ASSERT($initial.error.code=0)
ASSERT($initial.error.message="")
$batch:=SessionLock Events
assert_events($batch)
ASSERT($batch.events.length=0)
ASSERT($batch.dropped="0")
ASSERT(JSON Stringify($batch.state)=JSON Stringify($initial))
$state:=SessionLock State
ASSERT(JSON Stringify($state)=JSON Stringify($initial))

$stop:=SessionLock Stop
assert_result($stop)
ASSERT($stop.success=True)
ASSERT(JSON Stringify($stop.state)=JSON Stringify($initial))

$first:=SessionLock Start
assert_result($first)
ASSERT($first.state.generation="1")
If ($first.success)
    ASSERT($first.state.running=True)
    ASSERT($first.state.available=True)
    // Available means registered; a headless/macOS snapshot may still be unknown.
    $again:=SessionLock Start
    assert_result($again)
    ASSERT($again.success=True)
    ASSERT($again.state.running=True)
    ASSERT($again.state.available=True)
    ASSERT($again.state.generation=$first.state.generation)
Else
    assert_stopped($first.state)
    ASSERT($first.state.error.code#0)
End if

$state:=SessionLock State
assert_state($state)
ASSERT($state.running=$first.success)
ASSERT($state.generation=$first.state.generation)
$batch:=SessionLock Events
assert_events($batch)
ASSERT($batch.state.generation=$first.state.generation)

$stop:=SessionLock Stop
assert_result($stop)
assert_stopped($stop.state)
ASSERT($stop.success=($stop.state.error.code=0))
ASSERT($stop.state.generation=$first.state.generation)
// Stop retains pending events; drain while stopped, then verify an empty second drain.
$batch:=SessionLock Events
assert_events($batch)
assert_stopped($batch.state)
ASSERT($batch.state.sequence=$stop.state.sequence)
ASSERT($batch.state.droppedTotal=$stop.state.droppedTotal)
$batch:=SessionLock Events
assert_events($batch)
ASSERT($batch.events.length=0)
ASSERT($batch.dropped="0")
$state:=SessionLock State
assert_stopped($state)
ASSERT($state.sequence=$stop.state.sequence)
ASSERT($state.generation=$stop.state.generation)
$again:=SessionLock Stop
assert_result($again)
assert_stopped($again.state)
ASSERT($again.success=($again.state.error.code=0))
ASSERT($again.state.generation=$stop.state.generation)
ASSERT($again.state.sequence=$stop.state.sequence)
ASSERT($again.state.droppedTotal=$stop.state.droppedTotal)

$restart:=SessionLock Start
assert_result($restart)
ASSERT($restart.state.generation="2")
If ($restart.success)
    ASSERT($restart.state.running=True)
    ASSERT($restart.state.available=True)
Else
    assert_stopped($restart.state)
    ASSERT($restart.state.error.code#0)
End if
// Decimal counters remain strings: never coerce uint64 values to 4D numbers.
ASSERT((Length($restart.state.sequence)>Length($stop.state.sequence)) | ((Length($restart.state.sequence)=Length($stop.state.sequence)) & ($restart.state.sequence>=$stop.state.sequence)))
$stop:=SessionLock Stop
assert_result($stop)
assert_stopped($stop.state)
ASSERT($stop.success=($stop.state.error.code=0))
$batch:=SessionLock Events
assert_events($batch)
$batch:=SessionLock Events
assert_events($batch)
ASSERT($batch.events.length=0)
ASSERT($batch.dropped="0")
