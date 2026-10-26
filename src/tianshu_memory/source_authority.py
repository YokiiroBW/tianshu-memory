"""Authenticated owner snapshots joined with Memory's durable authority transaction."""

import json
import threading
from contextlib import contextmanager

from . import consolidation_sources as summary_sources
from . import segment_sources
from .domain import (
    Fault,
    SelectionChanged,
    admission_key,
    canonical,
    fingerprint,
    new_id,
    require,
    source_selector,
    utc,
)
from .postgres import is_postgres
from .read_lifecycle import begin_source_read, source_read_lock
from .source_lookup_migration import require_ready
from .source_order import SourceOperationOrder


class LocalRevisionChanged(Exception):
    """Internal retry signal; never an HTTP error or a successful sync result."""


class SourceAuthority:
    def __init__(self, transport, contracts):
        require(contracts.source_version == "1.0.0", "dependency_unavailable", 503)
        if contracts.source_batch_version is None:
            contracts.load_source_batches()
        require(contracts.source_batch_version == "1.0.0", "dependency_unavailable", 503)
        self.transport, self.contracts, self.rules = transport, contracts, contracts.source_rules
        # Serialize this owner's sync-and-business boundary, not SQLite across network I/O.
        # Otherwise independent readers repeatedly invalidate each other's global CAS.
        self._operation_lock = threading.RLock()
        self._operation_order = SourceOperationOrder()
        self._bundled = threading.local()

    @staticmethod
    def revision(db):
        state = require_ready(db)
        if is_postgres(db):
            require(
                state["source_revision"] is not None and state["source_recovery"] == "ready",
                "dependency_unavailable",
                503,
            )
            return int(state["source_revision"])
        row = db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()
        ready = db.execute("SELECT value FROM metadata WHERE key='source_recovery'").fetchone()
        require(
            row is not None and ready is not None and ready[0] == "ready",
            "dependency_unavailable",
            503,
        )
        return int(row[0])

    def coverage(self, db, scope, sources, profile, group_read=False):
        self.revision(db)
        selected = {
            canonical(source_selector(s, scope)): source_selector(s, scope) for s in sources
        }
        # Exact text domain includes historical/tombstoned dependencies. Profile domain covers
        # every active public/current-group projection, including other authors and targets.
        if group_read:
            # Include negatives and historical dependencies, not just currently active
            # projections. The requester is the viewer; admissions retain their owners.
            rows = db.execute(
                "SELECT a.selector FROM source_admissions a JOIN sources s ON s.key=a.key "
                "WHERE json_extract(s.scope,'$.actor_id')=? "
                "AND json_extract(s.scope,'$.audience')='group' "
                "AND json_extract(s.scope,'$.conversation_id')=?",
                (scope["actor_id"], scope["conversation_id"]),
            )
        elif profile:
            rows = db.execute(
                "SELECT DISTINCT a.selector FROM source_admissions a JOIN lineage l ON l.source_key=a.key "
                "JOIN groups g ON g.id=l.group_id JOIN profile_shares p ON p.group_id=g.id "
                "WHERE g.state='active' AND p.actor_id=? AND (p.sharing='public_preference' OR "
                "(p.sharing='group_only' AND ?='group' AND p.conversation_id=?))",
                (scope["actor_id"], scope["audience"], scope["conversation_id"]),
            )
        else:
            rows = db.execute(
                "SELECT a.selector FROM source_admissions a JOIN sources s ON s.key=a.key WHERE s.scope=?",
                (canonical(scope),),
            )
        for row in rows:
            selected[row[0]] = json.loads(row[0])
        return [selected[k] for k in sorted(selected)]

    @staticmethod
    def _read_domain(scope, *, profile=False, group_read=False):
        """Identity of a data query, never an authorization or source-check identity."""
        if group_read:
            return "group", scope["actor_id"], scope["conversation_id"]
        if profile:
            return "profile", scope["actor_id"], scope["audience"], scope["conversation_id"]
        return "exact", canonical(scope)

    def _relations(self, name, *args):
        try:
            return getattr(self.rules, name)(*args)
        except (ValueError, KeyError, TypeError):
            raise Fault("dependency_unavailable", 503) from None

    @staticmethod
    def batches(selectors, sources, scope):
        # Event inputs must share the first owner snapshot used by actor_event/check.
        requested = {canonical(source_selector(source, scope)) for source in sources}
        require(len(requested) <= 256, "dependency_unavailable", 503)
        ordered = sorted(
            selectors, key=lambda item: (canonical(item) not in requested, canonical(item))
        )
        return [ordered[index : index + 256] for index in range(0, len(ordered), 256)] or [[]]

    def _read_batch(self, selectors, turn_ids, viewer):
        request = dict(
            schema_version=1,
            request_id=new_id("facts"),
            mode="snapshot",
            selectors=selectors,
            turn_ids=turn_ids,
            include_content=False,
        )
        snapshot = self.transport.facts(request)
        self._relations("source_snapshot", request, snapshot)
        access_request, access = self._read_access(snapshot, viewer)
        return dict(
            request=request, snapshot=snapshot, access_request=access_request, access=access
        )

    def _read_access(self, snapshot, viewer):
        request = dict(
            schema_version=1,
            request_id=new_id("access"),
            operation="current",
            admissions=snapshot["admissions"],
            viewer=viewer,
        )
        access = self.transport.current(request)
        return request, access

    def _selected_plan(self, service, db, scope, read_plan):
        """Rebuild one internal proof plan inside both original CAS transactions."""
        extra_selectors = read_plan["source_selectors"]
        # Explicit/raw roots belong to the primary checkpoint. Each selected
        # root below already returns its complete alias closure independently.
        keys = segment_sources.equivalent_keys(db, {fingerprint(item) for item in extra_selectors})
        selectors = {canonical(item): item for item in extra_selectors}
        materials, manifests = {}, {}
        key_roots = {key: {None} for key in keys}
        window_roots, selections, checked_windows = {}, {}, set()

        def include(roots, required, saved):
            keys.update(required)
            for key in required:
                key_roots.setdefault(key, set()).update(roots)
            for item in saved:
                manifests[item["window_id"]] = item
                current = window_roots.setdefault(item["window_id"], set())
                if not roots <= current:
                    current.update(roots)
                    checked_windows.discard(item["window_id"])

        def dependencies(selection, roots):
            try:
                return summary_sources.selected_dependencies(service, db, selection)
            except SelectionChanged as error:
                raise SelectionChanged(root_ids=roots) from error

        for selection in read_plan["selected"]:
            value = {key: item for key, item in selection.items() if key != "root_id"}
            identity = canonical(value)
            if identity not in selections:
                selections[identity] = value, set()
            selections[identity][1].add(selection.get("root_id"))
        for selection, roots in selections.values():
            required, saved = dependencies(selection, roots)
            include(roots, required, saved)
        while True:
            unchecked = [
                item for identity, item in manifests.items() if identity not in checked_windows
            ]
            for manifest in unchecked:
                roots = window_roots[manifest["window_id"]]
                required, saved = dependencies(
                    dict(
                        scope=manifest["scope"],
                        group_ids=[],
                        episode_ids=[],
                        source_keys=[],
                        windows=[manifest],
                    ),
                    roots,
                )
                include(roots, required, saved)
                checked_windows.add(manifest["window_id"])
            for material in segment_sources.collect(
                db,
                scope,
                material_ids=read_plan["material_ids"],
                source_keys=keys,
                required_only=True,
            ):
                materials[material["id"]] = material
            checks = {}
            for material in materials.values():
                roots = set(key_roots.get(material["source_key"], {None}))
                if material["id"] in read_plan["material_ids"]:
                    roots.add(None)
                current, saved = segment_sources.requirements([material])
                checks.update((canonical(item), item) for item in current)
                include(roots, (), saved)
            material_checks = list(checks.values())
            if set(manifests) <= checked_windows:
                break
        ordered_keys = sorted(keys)
        for start in range(0, len(ordered_keys), 256):
            page = ordered_keys[start : start + 256]
            for row in db.execute(
                "SELECT selector FROM source_admissions WHERE key IN ("
                + ",".join("?" for _ in page)
                + ")",
                page,
            ):
                selectors[row[0]] = json.loads(row[0])
        for check in material_checks:
            for source in check["sources"]:
                selected = source_selector(source, check["scope"])
                selectors[canonical(selected)] = selected
        for selected in summary_sources.selectors(service, db, manifests.values()):
            selectors[canonical(selected)] = selected
        result = dict(
            selectors=[selectors[key] for key in sorted(selectors)],
            materials=[materials[key] for key in sorted(materials)],
            windows=[manifests[key] for key in sorted(manifests)],
        )
        return result, fingerprint(result)

    def barrier(
        self,
        service,
        scope,
        *,
        sources=(),
        context=None,
        profile=False,
        group_read=False,
        event=None,
        check=None,
        extra_selectors=(),
        windows=(),
        summary_scopes=(),
        source_checks=(),
        material_ids=(),
        dependency_keys=(),
        read_plan=None,
    ):
        require(
            not group_read
            or (
                context is not None
                and scope["audience"] == "group"
                and not profile
                and not sources
                and event is None
                and check is None
            ),
            "dependency_unavailable",
            503,
        )
        for _ in range(3):
            with service.store.transaction() as db:
                m0 = self.revision(db)
                planned = None
                if read_plan is not None:
                    planned, plan_digest = self._selected_plan(service, db, scope, read_plan)
                if read_plan is not None and not read_plan["full_read"]:
                    materials = planned["materials"]
                    all_windows = {item["window_id"]: item for item in planned["windows"]}
                    selectors = planned["selectors"]
                    material_checks, _ = segment_sources.requirements(materials)
                    summary_selectors = material_selectors = []
                else:
                    window_keys = [
                        item["source_key"]
                        for manifest in windows
                        for item in summary_sources.source_dependencies(manifest)
                    ]
                    materials = segment_sources.collect(
                        db,
                        scope,
                        summary_scopes,
                        material_ids=material_ids,
                        source_keys=(*dependency_keys, *window_keys),
                        group_read=group_read,
                        profile=profile,
                    )
                    material_checks, material_windows = segment_sources.requirements(materials)
                    material_selectors = [
                        source_selector(source, item["scope"])
                        for item in material_checks
                        for source in item["sources"]
                    ]
                    stored, window_domains = [], set()
                    for owner, shared in ((scope, group_read), *summary_scopes):
                        domain = self._read_domain(owner, group_read=shared)
                        if domain in window_domains:
                            continue
                        stored.extend(summary_sources.stored_windows(db, owner, group_read=shared))
                        window_domains.add(domain)
                    all_windows = {
                        item["window_id"]: item for item in (*stored, *windows, *material_windows)
                    }
                    summary_selectors = summary_sources.selectors(service, db, all_windows.values())
                    selectors = self.coverage(db, scope, sources, profile, group_read)
                    selectors = list(
                        {
                            canonical(value): value
                            for value in (
                                *selectors,
                                *extra_selectors,
                                *summary_selectors,
                                *material_selectors,
                            )
                        }.values()
                    )
                    if planned is not None:
                        materials = list(
                            {
                                item["id"]: item for item in (*materials, *planned["materials"])
                            }.values()
                        )
                        selected_checks, _ = segment_sources.requirements(planned["materials"])
                        material_checks = list(
                            {
                                canonical(item): item
                                for item in (*material_checks, *selected_checks)
                            }.values()
                        )
                        all_windows.update((item["window_id"], item) for item in planned["windows"])
                        summary_selectors = [*summary_selectors, *planned["selectors"]]
                        selectors = list(
                            {
                                canonical(item): item
                                for item in (*selectors, *planned["selectors"])
                            }.values()
                        )
                if context is not None:
                    service._authorize(db, context, scope=scope)
            turn_ids = list(
                dict.fromkeys(
                    [
                        *(
                            [event["aggregate_id"]]
                            if event
                            else [check["turn_id"]]
                            if check
                            else []
                        ),
                        *(item["turn_id"] for item in (*source_checks, *material_checks)),
                        *(
                            member["turn_id"]
                            for manifest in all_windows.values()
                            for member in manifest["members"]
                        ),
                    ]
                )
            )
            viewer = (
                None
                if context is None
                else {"origin": {"assertion_ref": context["assertion_ref"]}, "scope": scope}
            )
            observations = []
            passive_before = segment_sources.read(service, materials)
            window_checks = {
                identity: summary_sources.check_owner(self.transport, manifest)
                for identity, manifest in ((item["window_id"], item) for item in windows)
            }
            selector_batches = self.batches(selectors, sources, scope)
            for index in range(max(len(selector_batches), (len(turn_ids) + 31) // 32)):
                batch = selector_batches[index] if index < len(selector_batches) else []
                observation = self._read_batch(
                    batch, turn_ids[index * 32 : (index + 1) * 32], viewer
                )
                self._relations(
                    "current_access",
                    observation["access_request"],
                    observation["access"],
                    observation["snapshot"],
                    utc(service.clock()),
                )
                observations.append(observation)
            first = observations[0]
            # All batches must describe the same owner states. No prefix is committed.
            if any(
                item["snapshot"]["head"] != first["snapshot"]["head"]
                or item["access"]["head"] != first["access"]["head"]
                for item in observations
            ):
                continue
            final_access_request, final_access = first["access_request"], first["access"]
            if len(observations) > 1:
                final_access_request, final_access = self._read_access({"admissions": []}, viewer)
                self._relations(
                    "current_access",
                    final_access_request,
                    final_access,
                    {"admissions": [], "physicals": []},
                    utc(service.clock()),
                )
                if final_access["head"] != first["access"]["head"]:
                    continue
            final_request = dict(
                schema_version=1,
                request_id=new_id("head"),
                mode="head",
                selectors=[],
                turn_ids=[],
                include_content=False,
            )
            final = self.transport.facts(final_request)
            self._relations("source_snapshot", final_request, final)
            if first["snapshot"]["head"] != final["head"]:
                if len(observations) != 1 or not self._refresh_unchanged(service, first, final):
                    continue
                final = first["snapshot"]
            for observation in observations:
                observation["final_head"] = final["head"]
                current = observation["access"]["viewer_context"]
                if context is not None:
                    require(
                        current["verified_account"] == context["verified_account"]
                        and current["verified_channel"] == context["verified_channel"]
                        and current == final_access["viewer_context"],
                        "dependency_unavailable",
                        503,
                    )
            passive_after = segment_sources.read(service, materials)
            if not segment_sources.same(passive_before, passive_after):
                continue
            try:
                if len(observations) == 1:
                    result = self.sync(
                        service,
                        first,
                        m0=m0,
                        scope=scope,
                        sources=sources,
                        profile=profile,
                        group_read=group_read,
                        extra_selectors=(*extra_selectors, *summary_selectors, *material_selectors),
                        read_plan=read_plan,
                        plan_digest=plan_digest if read_plan is not None else None,
                    )
                else:
                    result = self.sync_batches(
                        service,
                        dict(
                            schema_version=1,
                            observations=observations,
                            final_access_request=final_access_request,
                            final_access=final_access,
                            final_core_head=final["head"],
                        ),
                        m0=m0,
                        scope=scope,
                        sources=sources,
                        profile=profile,
                        group_read=group_read,
                        extra_selectors=(*extra_selectors, *summary_selectors, *material_selectors),
                        read_plan=read_plan,
                        plan_digest=plan_digest if read_plan is not None else None,
                    )
            except LocalRevisionChanged:
                continue
            if passive_after:
                with service.store.transaction() as db:
                    if self.revision(db) != result["local_revision"]:
                        continue
                    segment_sources.apply(service, db, passive_after)
                    result["local_revision"] = self.revision(db)
            if all_windows:
                with service.store.transaction() as db:
                    if self.revision(db) != result["local_revision"]:
                        continue
                    current_turns = {
                        turn["turn_id"]: turn
                        for item in observations
                        for turn in item["snapshot"]["turns"]
                    }
                    failures = summary_sources.window_failures(
                        service, db, all_windows.values(), current_turns, window_checks
                    )
                    invalid = [
                        identity for identity, failure in failures.items() if failure is not None
                    ]
                    summary_sources.invalidate_windows(service, db, invalid)
                    result["local_revision"] = self.revision(db)
                for item in windows:
                    failure = failures[item["window_id"]]
                    if failure is not None:
                        raise failure
            # Negatives are durable before a later bad event or scope conflict is rejected.
            snapshot = first["snapshot"]
            if event:
                turn = next(
                    item for item in snapshot["turns"] if item["turn_id"] == event["aggregate_id"]
                )
                self._relations("actor_event", event, turn, snapshot)
            if check:
                self._check_input_sources(check, snapshot)
            if source_checks or material_checks:
                # These are the actual same-head observations already validated
                # and committed above. A raw turn may be in any 32-turn page,
                # and its original sources may be in any 256-selector page.
                complete = self._merge_snapshot(observations)
                for source_check in (*source_checks, *material_checks):
                    self._check_input_sources(source_check, complete)
            return result, final_access.get("viewer_context")
        raise Fault("dependency_unavailable", 503)

    def _check_input_sources(self, check, snapshot):
        turn = next(
            (item for item in snapshot["turns"] if item["turn_id"] == check["turn_id"]), None
        )
        require(
            turn is not None
            and turn["scope"] == check["scope"]
            and turn["input_revision"] == check["input_revision"]
            and turn["input_sources"] == list(check["sources"]),
            "dependency_unavailable",
            503,
        )
        self._relations("current_actor_sources", check["scope"], check["sources"], snapshot)

    def _refresh_unchanged(self, service, observation, final):
        """Double-collect the requested facts when an unrelated Core write moves its head.

        The Platform read sits between the two Core snapshots. Equal versioned facts
        prove the relevant sources were stable across that authorization read. Keep the
        actual later snapshot (including its request digest/head); never synthesize a
        head or reuse this proof for multiple batches. Changed/withdrawn facts retry the
        complete barrier so negatives and access are applied together.
        """
        before = observation["snapshot"]
        head = before["head"]
        with service.store.transaction() as db:
            old = db.execute("SELECT * FROM owner_heads WHERE owner='core'").fetchone()
            if old:
                require(
                    head["generation"] == old["generation"] and head["sequence"] >= old["sequence"],
                    "dependency_unavailable",
                    503,
                )
        require(
            final["head"]["generation"] == head["generation"]
            and final["head"]["sequence"] >= head["sequence"],
            "dependency_unavailable",
            503,
        )
        after = self.transport.facts(observation["request"])
        self._relations("source_snapshot", observation["request"], after)
        require(
            after["head"]["generation"] == head["generation"]
            and after["head"]["sequence"] >= final["head"]["sequence"],
            "dependency_unavailable",
            503,
        )
        if any(before[key] != after[key] for key in ("physicals", "admissions", "turns")):
            return False
        observation["snapshot"] = after
        return True

    def sync(
        self,
        service,
        observation,
        *,
        m0,
        scope,
        sources=(),
        profile=False,
        group_read=False,
        extra_selectors=(),
        read_plan=None,
        plan_digest=None,
    ):
        """Trusted source-sync port. m0 and scope are local state, never wire authority."""
        self.contracts.validate("sync-workflow#sync_input", observation)
        return self._commit_observations(
            service,
            [observation],
            m0=m0,
            scope=scope,
            sources=sources,
            profile=profile,
            group_read=group_read,
            extra_selectors=extra_selectors,
            read_plan=read_plan,
            plan_digest=plan_digest,
        )

    def sync_batches(
        self,
        service,
        observation,
        *,
        m0,
        scope,
        sources=(),
        profile=False,
        group_read=False,
        extra_selectors=(),
        read_plan=None,
        plan_digest=None,
    ):
        self.contracts.validate("sync-batch#barrier", observation)
        try:
            self.contracts.source_batch_rules.batch_barrier(observation, utc(service.clock()))
        except (ValueError, KeyError, TypeError):
            raise Fault("dependency_unavailable", 503) from None
        return self._commit_observations(
            service,
            observation["observations"],
            m0=m0,
            scope=scope,
            sources=sources,
            profile=profile,
            group_read=group_read,
            extra_selectors=extra_selectors,
            read_plan=read_plan,
            plan_digest=plan_digest,
        )

    def _commit_observations(
        self,
        service,
        observations,
        *,
        m0,
        scope,
        sources,
        profile,
        group_read=False,
        extra_selectors=(),
        read_plan=None,
        plan_digest=None,
    ):
        for observation in observations:
            self._relations("sync_barrier", dict(observation, now=utc(service.clock())))
        selectors = sorted(
            (selector for item in observations for selector in item["request"]["selectors"]),
            key=canonical,
        )
        first = observations[0]
        snapshot = self._merge_snapshot(observations)
        access = dict(
            first["access"], grants=[g for item in observations for g in item["access"]["grants"]]
        )
        with service.store.transaction() as db:
            if self.revision(db) != m0:
                raise LocalRevisionChanged()
            if read_plan is not None:
                planned, current_digest = self._selected_plan(service, db, scope, read_plan)
                if current_digest != plan_digest or (
                    not read_plan["full_read"] and planned["selectors"] != selectors
                ):
                    raise LocalRevisionChanged()
            if (read_plan is None or read_plan["full_read"]) and sorted(
                {
                    canonical(value): value
                    for value in (
                        *self.coverage(db, scope, sources, profile, group_read),
                        *extra_selectors,
                    )
                }.values(),
                key=canonical,
            ) != selectors:
                raise LocalRevisionChanged()
            viewer = access["viewer_context"]
            if viewer is not None:
                service._authorize(db, viewer, scope=scope)
            result = self._apply(service, db, snapshot, access)
            self.contracts.validate("sync-workflow#sync_result", result)
        return result

    @staticmethod
    def _merge_snapshot(observations):
        """Join actual pages using the same physical and receipt consistency rules."""
        physicals = {}
        receipts = set()
        for observation in observations:
            for physical in observation["snapshot"]["physicals"]:
                key = fingerprint(physical["key"])
                require(
                    key not in physicals or physicals[key] == physical,
                    "dependency_unavailable",
                    503,
                )
                physicals[key] = physical
            for admission in observation["snapshot"]["admissions"]:
                receipt = admission["source"]["receipt_id"]
                require(receipt not in receipts, "dependency_unavailable", 503)
                receipts.add(receipt)
        return dict(
            observations[0]["snapshot"],
            physicals=list(physicals.values()),
            admissions=[a for item in observations for a in item["snapshot"]["admissions"]],
            turns=[turn for item in observations for turn in item["snapshot"]["turns"]],
        )

    @contextmanager
    def local_commit(self, service):
        """Serialize a local writer's entire transaction without a remote barrier.

        This owns a new outer transaction. Helpers already borrowing a business
        database keep that database rather than entering another transaction.
        Commit/checkpoint persistence and rollback finish before either owner
        is released, in the same order as the source operation boundaries.
        """
        with self._operation_order.turn(), self._operation_lock:
            with service.store.transaction() as db:
                yield db

    @contextmanager
    def operation(self, service, scope, **kwargs):
        active = getattr(self._bundled, "active", None)
        if active is not None:
            key = (canonical(scope), bool(kwargs.get("profile")), bool(kwargs.get("group_read")))
            require(
                active["service"] is service
                and key in active["domains"]
                and not any(
                    kwargs.get(name)
                    for name in (
                        "sources",
                        "event",
                        "check",
                        "source_checks",
                        "material_ids",
                        "dependency_keys",
                        "read_plan",
                    )
                ),
                "dependency_unavailable",
                503,
            )
            if kwargs.get("context") is not None:
                service._authorize(active["db"], kwargs["context"], scope=scope)
            yield active["db"]
            return
        with self._operation_order.turn(), source_read_lock(self._operation_lock):
            begin_source_read()
            with self._operation(service, scope, **kwargs) as db:
                yield db

    @contextmanager
    def bundle_operation(self, service, requirements):
        """One full union barrier and transaction for authenticated scope requirements.

        Requirements are assembled by the authenticated application, never accepted
        from a model or interpreted as grants. Existing component rules still run.
        """
        require(
            bool(requirements) and getattr(self._bundled, "active", None) is None,
            "invalid_input",
            400,
        )
        with self._operation_order.turn(), source_read_lock(self._operation_lock):
            begin_source_read()
            selectors, exact_selectors, domains, source_checks, read_domains = (
                {},
                {},
                set(),
                [],
                set(),
            )
            dependency_keys, material_ids = set(), set()
            # A legacy/full component keeps the entire bundle on the original
            # full path, including its durable per-window negative handling.
            full_read = any(not item.get("selected_only") for item in requirements)
            selected_plans = {}
            with service.store.transaction() as db:
                for item in requirements:
                    scope = item["scope"]
                    dependency_keys.update(item.get("dependency_keys", ()))
                    material_ids.update(item.get("material_ids", ()))
                    if full_read and item.get("selected") is not None:
                        dependency_keys.update(item["selected"]["source_keys"])
                    if item.get("context") is not None:
                        service._authorize(db, item["context"], scope=scope)
                    profile, shared = bool(item.get("profile")), bool(item.get("group_read"))
                    if item.get("selected") is not None:
                        selected_plans[canonical(item["selected"])] = item["selected"]
                    if full_read:
                        domains.add((canonical(scope), profile, shared))
                    domain = self._read_domain(scope, profile=profile, group_read=shared)
                    if full_read and domain not in read_domains:
                        for selected in self.coverage(db, scope, (), profile, shared):
                            selectors[canonical(selected)] = selected
                        read_domains.add(domain)
                    for check in item.get("source_checks", ()):
                        require(check["scope"] == scope, "invalid_input", 400)
                        source_checks.append(check)
                        for source in check["sources"]:
                            selected = source_selector(source, scope)
                            selectors[canonical(selected)] = selected
                            exact_selectors[canonical(selected)] = selected
            primary = requirements[0]
            read_plan = (
                dict(
                    selected=list(selected_plans.values()),
                    source_selectors=list(exact_selectors.values()),
                    material_ids=sorted(material_ids),
                    full_read=full_read,
                )
                if selected_plans
                else None
            )
            with self._operation(
                service,
                primary["scope"],
                context=primary.get("context"),
                profile=bool(primary.get("profile")),
                group_read=bool(primary.get("group_read")),
                extra_selectors=list(selectors.values()),
                summary_scopes=[
                    (item["scope"], bool(item.get("group_read"))) for item in requirements
                ],
                source_checks=source_checks,
                dependency_keys=dependency_keys,
                material_ids=material_ids,
                read_plan=read_plan,
            ) as db:
                self._bundled.active = {"service": service, "domains": domains, "db": db}
                try:
                    yield db
                finally:
                    self._bundled.active = None

    @contextmanager
    def _operation(self, service, scope, **kwargs):
        for _ in range(3):
            result, viewer = self.barrier(service, scope, **kwargs)
            with service.store.transaction() as db:
                if self.revision(db) != result["local_revision"]:
                    continue
                if viewer is not None:
                    service._authorize(db, viewer, scope=scope)
                yield db
                return
        raise Fault("dependency_unavailable", 503)

    def _apply(self, service, db, snapshot, access):
        batch = None
        if is_postgres(db):
            from .postgres_source_snapshot import SourceSnapshot

            batch = SourceSnapshot(db, snapshot, access)
        heads = {"core": snapshot["head"], "platform": access["head"]}
        for owner, head in heads.items():
            old = (
                db.execute("SELECT * FROM owner_heads WHERE owner=?", (owner,)).fetchone()
                if batch is None
                else batch.heads.get(owner)
            )
            if old:
                require(
                    head["generation"] == old["generation"] and head["sequence"] >= old["sequence"],
                    "dependency_unavailable",
                    503,
                )
        physicals = {fingerprint(p["key"]): p for p in snapshot["physicals"]}
        grants = {fingerprint(g["selector"]): g for g in access["grants"]}
        # The last observed sequence is per object: another scope may already have advanced
        # the global head. At the same object sequence a changed owner fact is equivocation.
        for kind, owner, values in (
            ("physical", "core", physicals),
            ("admission", "core", {fingerprint(a["selector"]): a for a in snapshot["admissions"]}),
            ("turn", "core", {t["turn_id"]: t for t in snapshot["turns"]}),
            ("access", "platform", grants),
        ):
            for key, value in values.items():
                old = (
                    db.execute(
                        "SELECT * FROM source_observations WHERE kind=? AND key=?", (kind, key)
                    ).fetchone()
                    if batch is None
                    else batch.observations.get((kind, key))
                )
                if old and old["sequence"] == heads[owner]["sequence"]:
                    retained = json.loads(old["payload"])
                    observed = value
                    if kind == "access":
                        # This digest binds the response to the incoming Core admission.
                        # A Core revision can change it without changing Platform facts.
                        # current_access has already verified that request correlation.
                        retained = {k: v for k, v in retained.items() if k != "admission_digest"}
                        observed = {k: v for k, v in value.items() if k != "admission_digest"}
                    require(retained == observed, "dependency_unavailable", 503)
                payload = canonical(value)
                if (
                    batch is None
                    or old is None
                    or old["sequence"] != heads[owner]["sequence"]
                    or old["payload"] != payload
                ):
                    db.execute(
                        "INSERT INTO source_observations VALUES (?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET "
                        "sequence=excluded.sequence,payload=excluded.payload "
                        "WHERE source_observations.sequence != excluded.sequence OR source_observations.payload != excluded.payload",
                        (kind, key, heads[owner]["sequence"], payload),
                    )
        # Local binding is an independent authority. A remote allowed bit cannot mint a person.
        for admission in snapshot["admissions"]:
            physical = physicals[fingerprint(admission["selector"]["key"])]
            binding = (
                service._binding(db, physical["author"])
                if batch is None
                else batch.bindings.get(canonical(physical["author"]))
            )
            require(
                binding is not None
                and binding["person_id"] == admission["scope"]["person_id"]
                and binding["version"] == admission["binding_version"],
                "dependency_unavailable",
                503,
            )
            require(
                (
                    db.execute(
                        "SELECT 1 FROM source_admissions WHERE payload IS NOT NULL "
                        "AND json_extract(payload,'$.source.receipt_id')=? AND key!=? LIMIT 1",
                        (admission["source"]["receipt_id"], fingerprint(admission["selector"])),
                    ).fetchone()
                    is None
                    if batch is None
                    else not (
                        batch.receipts.get(admission["source"]["receipt_id"], set())
                        - {fingerprint(admission["selector"])}
                    )
                ),
                "dependency_unavailable",
                503,
            )
        invalid_keys = set()
        for key, physical in physicals.items():
            old = (
                db.execute("SELECT * FROM physical_sources WHERE key=?", (key,)).fetchone()
                if batch is None
                else batch.physicals.get(key)
            )
            if old:
                require(physical["revision"] >= old["revision"], "dependency_unavailable", 503)
                require(
                    old["state"] != "withdrawn" or physical["state"] == "withdrawn",
                    "dependency_unavailable",
                    503,
                )
                if old["payload"] is not None:
                    saved = json.loads(old["payload"])
                    require(
                        all(
                            saved[k] == physical[k]
                            for k in ("key", "author", "audience", "conversation_id")
                        ),
                        "dependency_unavailable",
                        503,
                    )
                    if saved["revision"] == physical["revision"]:
                        require(
                            all(
                                saved[k] == physical[k]
                                for k in ("content_digest", "kind", "physical_receipt_id")
                            ),
                            "dependency_unavailable",
                            503,
                        )
                    if saved != physical:
                        invalid_keys.update(
                            (
                                r[0]
                                for r in db.execute(
                                    "SELECT key FROM source_admissions WHERE physical_key=?", (key,)
                                )
                            )
                            if batch is None
                            else batch.aliases.get(key, ())
                        )
            payload = canonical(physical)
            if (
                batch is None
                or old is None
                or old["payload"] != payload
                or old["revision"] != physical["revision"]
                or old["state"] != physical["state"]
            ):
                db.execute(
                    "INSERT INTO physical_sources VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "payload=excluded.payload,revision=excluded.revision,state=excluded.state "
                    "WHERE physical_sources.payload IS NOT excluded.payload OR physical_sources.revision != excluded.revision "
                    "OR physical_sources.state != excluded.state",
                    (key, payload, physical["revision"], physical["state"]),
                )
        # The physical negative broadcast includes all locally known actors, without obtaining
        # their positive grants or exposing their lineage in this actor's response.
        for key in invalid_keys:
            db.execute("UPDATE sources SET state='withdrawn',epoch=epoch+1 WHERE key=?", (key,))
            if batch is not None:
                batch.withdraw(key)
        for admission in snapshot["admissions"]:
            key, pk = fingerprint(admission["selector"]), fingerprint(admission["selector"]["key"])
            physical, grant = physicals[pk], grants[key]
            source, scope = admission["source"], admission["scope"]
            old = (
                db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
                if batch is None
                else batch.sources.get(key)
            )
            suppression = (
                db.execute("SELECT 1 FROM suppression WHERE source_key=?", (key,)).fetchone()
                if batch is None
                else key in batch.suppressed
            )
            active = (
                grant["state"] == "allowed"
                and physical["state"] == "active"
                and source["message_key"]["revision"] == physical["revision"]
                and admission["physical_receipt_id"] == physical["physical_receipt_id"]
                and physical["classification"]["value"] in {"real", "fictional"}
                and not suppression
            )
            state = "active" if active else "withdrawn"
            reality = physical["classification"]["value"]
            if old:
                require(old["scope"] == canonical(scope), "dependency_unavailable", 503)
                require(
                    source["message_key"]["revision"] >= old["revision"],
                    "dependency_unavailable",
                    503,
                )
                stored_admission = (
                    db.execute(
                        "SELECT payload FROM source_admissions WHERE key=?", (key,)
                    ).fetchone()
                    if batch is None
                    else (batch.admissions[key]["payload"],)
                    if key in batch.admissions
                    else None
                )
                if (
                    stored_admission
                    and stored_admission[0]
                    and old["revision"] == source["message_key"]["revision"]
                ):
                    require(
                        json.loads(stored_admission[0]) == admission, "dependency_unavailable", 503
                    )
                changed = (
                    old["payload"] != canonical(source)
                    or old["reality"] != reality
                    or (old["state"] == "active" and state != "active")
                )
                epoch = old["epoch"]
                if changed and key not in invalid_keys:
                    invalid_keys.add(key)
                    epoch += 1
                values = (
                    source["message_key"]["revision"],
                    epoch,
                    state,
                    canonical(scope),
                    reality,
                    canonical(source),
                )
                if values != tuple(
                    old[k] for k in ("revision", "epoch", "state", "scope", "reality", "payload")
                ):
                    db.execute(
                        "UPDATE sources SET revision=?,epoch=?,state=?,scope=?,reality=?,payload=? WHERE key=?",
                        (*values, key),
                    )
            else:
                db.execute(
                    "INSERT INTO sources VALUES (?,?,1,?,?,?,?)",
                    (
                        key,
                        source["message_key"]["revision"],
                        state,
                        canonical(scope),
                        reality,
                        canonical(source),
                    ),
                )
            previous = batch.admissions.get(key) if batch is not None else None
            payload = canonical(admission)
            if (
                batch is None
                or previous is None
                or previous["payload"] != payload
                or previous["verified"] != 1
            ):
                db.execute(
                    "INSERT INTO source_admissions VALUES (?,?,?,?,?,1) ON CONFLICT(key) DO UPDATE SET "
                    "payload=excluded.payload,verified=1 WHERE source_admissions.payload IS NOT excluded.payload OR source_admissions.verified != 1",
                    (key, pk, scope["actor_id"], canonical(admission["selector"]), payload),
                )
        service._invalidate_sources(db, invalid_keys)
        for owner, head in heads.items():
            old = batch.heads.get(owner) if batch is not None else None
            if (
                batch is None
                or old is None
                or old["generation"] != head["generation"]
                or old["sequence"] != head["sequence"]
            ):
                db.execute(
                    "INSERT INTO owner_heads VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                    "generation=excluded.generation,sequence=excluded.sequence "
                    "WHERE owner_heads.generation != excluded.generation OR owner_heads.sequence != excluded.sequence",
                    (owner, head["generation"], head["sequence"]),
                )
        return {
            "local_revision": self.revision(db),
            "core_head": heads["core"],
            "platform_head": heads["platform"],
        }

    def verify(self, db, sources, scope):
        for source in sources:
            row = db.execute(
                "SELECT * FROM source_admissions WHERE key=?", (admission_key(source, scope),)
            ).fetchone()
            require(row is not None and row["verified"], "dependency_unavailable", 503)

    def current(self, db, rows):
        return bool(rows) and all(
            row["state"] == "active"
            and row["revision"] == row["required_revision"]
            and row["epoch"] == row["required_epoch"]
            and (
                db.execute(
                    "SELECT 1 FROM source_admissions WHERE key=? AND verified=1", (row["key"],)
                ).fetchone()
                or segment_sources.admitted(db, row["key"])
            )
            and not db.execute(
                "SELECT 1 FROM suppression WHERE source_key=?", (row["key"],)
            ).fetchone()
            for row in rows
        )

    def current_many(self, db, groups):
        """The same admission/suppression rule over this transaction's lineage batch.

        Nothing survives the operation or changes synchronization/revision handling.
        Inactive/old source rows are excluded before reading record payloads.
        """
        candidates = {
            identity: rows
            for identity, rows in groups.items()
            if rows
            and all(
                row["state"] == "active"
                and row["revision"] == row["required_revision"]
                and row["epoch"] == row["required_epoch"]
                for row in rows
            )
        }
        keys = list({row["key"] for rows in candidates.values() for row in rows})
        admitted = set()
        for offset in range(0, len(keys), 256):
            batch = keys[offset : offset + 256]
            admitted.update(
                row[0]
                for row in db.execute(
                    "SELECT a.key FROM source_admissions a WHERE a.verified=1 "
                    "AND a.key IN (" + ",".join("?" for _ in batch) + ") "
                    "AND NOT EXISTS (SELECT 1 FROM suppression s WHERE s.source_key=a.key)",
                    batch,
                )
            )
            if any(key.startswith("observation:") for key in batch) and segment_sources.installed(
                db
            ):
                admitted.update(
                    row[0]
                    for row in db.execute(
                        "SELECT m.source_key FROM memory_materials m WHERE m.kind='observation' "
                        "AND m.verified=1 AND m.source_key IN ("
                        + ",".join("?" for _ in batch)
                        + ") "
                        "AND NOT EXISTS (SELECT 1 FROM suppression s WHERE s.source_key=m.source_key)",
                        batch,
                    )
                )
        return {
            identity
            for identity, rows in candidates.items()
            if all(row["key"] in admitted for row in rows)
        }
