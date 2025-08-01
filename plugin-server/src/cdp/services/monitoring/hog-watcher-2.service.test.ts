jest.mock('~/utils/posthog', () => {
    return {
        captureTeamEvent: jest.fn(),
    }
})

import { Hub, ProjectId, Team } from '../../../types'
import { closeHub, createHub } from '../../../utils/db/hub'
import { createExampleInvocation, createHogFunction } from '../../_tests/fixtures'
import { deleteKeysWithPrefix } from '../../_tests/redis'
import { CdpRedis, createCdpRedisPool } from '../../redis'
import { CyclotronJobInvocationHogFunction, CyclotronJobInvocationResult, HogFunctionType } from '../../types'
import { createInvocationResult } from '../../utils/invocation-utils'
import { BASE_REDIS_KEY, HogWatcherService2, HogWatcherStateEnum } from './hog-watcher-2.service'

const mockNow: jest.SpyInstance = jest.spyOn(Date, 'now')
const mockCaptureTeamEvent: jest.Mock = require('~/utils/posthog').captureTeamEvent as any

describe('HogWatcher', () => {
    let now: number
    let hub: Hub
    let watcher: HogWatcherService2
    let onStateChangeSpy: jest.SpyInstance
    let redis: CdpRedis
    const hogFunctionId: string = 'hog-function-id'
    let hogFunction: HogFunctionType

    let team: Team

    beforeAll(async () => {
        team = {
            id: 2,
            project_id: 1 as ProjectId,
            uuid: 'test-uuid',
            organization_id: 'organization-id',
            name: 'testTeam',
        } as Team
        hub = await createHub()
        jest.spyOn(hub.teamManager, 'getTeam').mockResolvedValue(team)
        redis = createCdpRedisPool(hub)
        process.env.CDP_HOG_WATCHER_2_ENABLED = 'true'
        process.env.CDP_HOG_WATCHER_2_CAPTURE_ENABLED = 'true'
    })

    beforeEach(async () => {
        now = 1720000000000
        mockNow.mockReturnValue(now)
        await deleteKeysWithPrefix(redis, BASE_REDIS_KEY)
        hub.CDP_WATCHER_AUTOMATICALLY_DISABLE_FUNCTIONS = true

        watcher = new HogWatcherService2(hub, redis)
        onStateChangeSpy = jest.spyOn(watcher as any, 'onStateChange') as jest.SpyInstance
        hogFunction = createHogFunction({ id: hogFunctionId, team_id: 2 })
    })

    afterAll(async () => {
        await closeHub(hub)
    })

    const createResult = (options: {
        id?: string
        duration?: number
        finished?: boolean
        error?: string
        kind?: 'hog' | 'async_function'
    }): CyclotronJobInvocationResult<CyclotronJobInvocationHogFunction> => {
        const invocation = createExampleInvocation({ id: options.id ?? hogFunctionId, team_id: 2 })
        invocation.state.timings = [
            {
                kind: options.kind ?? 'hog',
                duration_ms: options.duration ?? 0,
            },
        ]

        return createInvocationResult(
            invocation,
            {},
            {
                finished: options.finished ?? true,
                error: options.error,
            }
        )
    }

    const advanceTime = (ms: number) => {
        now += ms
        mockNow.mockReturnValue(now)
    }

    describe('constructor', () => {
        it('should validate the bounds configuration', () => {
            expect(() => {
                const _badWatcher = new HogWatcherService2(
                    {
                        ...hub,
                        CDP_WATCHER_HOG_COST_TIMING_LOWER_MS: 100,
                        CDP_WATCHER_HOG_COST_TIMING_UPPER_MS: 100,
                        CDP_WATCHER_HOG_COST_TIMING: 1,
                        CDP_WATCHER_ASYNC_COST_TIMING_LOWER_MS: 100,
                        CDP_WATCHER_ASYNC_COST_TIMING_UPPER_MS: 100,
                        CDP_WATCHER_ASYNC_COST_TIMING: 1,
                    },
                    redis
                )
            }).toThrow(
                'Lower bound for kind hog of 100ms must be lower than upper bound of 100ms. This is a configuration error.'
            )
        })
    })

    describe('observeResults', () => {
        const cases: [
            { name: string; cost: number; state: number },
            CyclotronJobInvocationResult<CyclotronJobInvocationHogFunction>[]
        ][] = [
            [
                { name: 'should calculate cost and state for single default result', cost: 0, state: 1 },
                [createResult({})],
            ],
            [
                { name: 'should calculate cost and state for multiple default results', cost: 0, state: 1 },
                [createResult({}), createResult({}), createResult({})],
            ],
            [
                { name: 'should calculate cost and state for small durations', cost: 0, state: 1 },
                [createResult({ duration: 10 }), createResult({ duration: 20 }), createResult({ duration: 30 })],
            ],
            [
                { name: 'should calculate cost and state for medium durations', cost: 12, state: 1 },
                [
                    createResult({ duration: 1000, kind: 'async_function' }),
                    createResult({ duration: 1000, kind: 'async_function' }),
                    createResult({ duration: 1000, kind: 'async_function' }),
                ],
            ],
            [
                { name: 'should calculate cost and state for single large duration', cost: 20, state: 1 },
                [createResult({ duration: 5000, kind: 'async_function' })],
            ],
            [
                { name: 'should calculate cost and state for single very large duration', cost: 40, state: 1 },
                [createResult({ duration: 10000, kind: 'async_function' })],
            ],
            [
                {
                    name: 'should calculate cumulative cost and state for multiple large durations',
                    cost: 141,
                    state: 1,
                },
                [
                    createResult({ duration: 5000, kind: 'async_function' }),
                    createResult({ duration: 10000, kind: 'async_function' }),
                    createResult({ duration: 20000, kind: 'async_function' }),
                ],
            ],
        ]

        it.each(cases.map(([meta, results]) => [meta.name, meta, results]))(
            '%s',
            async (name, expectedScore, results) => {
                await watcher.observeResults(results)
                const result = await watcher.getPersistedState(hogFunctionId)
                expect(hub.CDP_WATCHER_BUCKET_SIZE - result.tokens).toEqual(expectedScore.cost)
                expect(result.state).toEqual(expectedScore.state)
            }
        )

        it('should calculate costs per individual timing not based on total duration', async () => {
            // Create a result with multiple timings that would have different costs
            // if calculated individually vs. summed together
            const result = createResult({
                id: 'id1',
                finished: true,
                kind: 'async_function',
            }) as CyclotronJobInvocationResult<CyclotronJobInvocationHogFunction>

            // Replace the default timing with multiple timings
            result.invocation.state.timings = [
                { kind: 'async_function', duration_ms: 90 }, // Below threshold, should have minimal cost
                { kind: 'async_function', duration_ms: 90 }, // Below threshold, should have minimal cost
                { kind: 'async_function', duration_ms: 90 }, // Below threshold, should have minimal cost
            ]

            // If using individual timings (correct): each timing has a small cost
            // If using total duration (incorrect): 300ms total would have a higher cost

            await watcher.observeResults([result])
            const state = await watcher.getPersistedState(hogFunctionId)

            // Expected: each 100ms timing has minimal cost since it's below the lower threshold
            // This is checking that we're not summing them into a 300ms duration
            const expectedIndividualCost = 0 // Three 100ms timings each have minimal/zero cost
            const totalCost = hub.CDP_WATCHER_BUCKET_SIZE - state.tokens

            expect(totalCost).toEqual(expectedIndividualCost)
        })

        it('should max out scores', async () => {
            let lotsOfResults = Array(10000).fill(createResult({ duration: 25000, kind: 'async_function' }))

            await watcher.observeResults(lotsOfResults)

            expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                {
                  "state": 3,
                  "tokens": -1,
                }
            `)

            lotsOfResults = Array(10000).fill(createResult({ id: 'id2', kind: 'async_function' }))

            await watcher.observeResults(lotsOfResults)

            expect(await watcher.getPersistedState('id2')).toMatchInlineSnapshot(`
                {
                  "state": 1,
                  "tokens": 10000,
                }
            `)
        })

        it('should refill over time', async () => {
            hub.CDP_WATCHER_REFILL_RATE = 10
            await watcher.observeResults([
                createResult({ duration: 10000, kind: 'async_function' }),
                createResult({ duration: 10000, kind: 'async_function' }),
                createResult({ duration: 10000, kind: 'async_function' }),
            ])

            expect((await watcher.getPersistedState(hogFunctionId)).tokens).toMatchInlineSnapshot(`9880`)
            advanceTime(1000)
            expect((await watcher.getPersistedState(hogFunctionId)).tokens).toMatchInlineSnapshot(`9890`)
            advanceTime(10000)
            expect((await watcher.getPersistedState(hogFunctionId)).tokens).toMatchInlineSnapshot(`9990`)
        })

        describe('onStateChange', () => {
            it('should trigger state change events', async () => {
                await watcher.clearLock(hogFunctionId) // For testing the logic
                await watcher.observeResults(Array(10).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 1,
                      "tokens": 8100,
                    }
                `)
                expect(onStateChangeSpy).toHaveBeenCalledTimes(0)

                await watcher.clearLock(hogFunctionId) // For testing the logic
                await watcher.observeResults(Array(10).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 2,
                      "tokens": 6200,
                    }
                `)
                expect(onStateChangeSpy).toHaveBeenCalledTimes(1) // New state change
                expect(onStateChangeSpy).toHaveBeenLastCalledWith({
                    hogFunction,
                    state: HogWatcherStateEnum.degraded,
                    previousState: HogWatcherStateEnum.healthy,
                })

                await watcher.clearLock(hogFunctionId) // For testing the logic
                await watcher.observeResults(Array(10).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 2,
                      "tokens": 4300,
                    }
                `)
                expect(onStateChangeSpy).toHaveBeenCalledTimes(1) // NO New state change

                await watcher.clearLock(hogFunctionId) // For testing the logic
                await watcher.observeResults(Array(100).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 3,
                      "tokens": -1,
                    }
                `)
                expect(onStateChangeSpy).toHaveBeenCalledTimes(2) // New state change
                expect(onStateChangeSpy).toHaveBeenLastCalledWith({
                    hogFunction,
                    state: HogWatcherStateEnum.disabled,
                    previousState: HogWatcherStateEnum.degraded,
                })
            })

            it('should not transition to disabled if not enabled', async () => {
                hub.CDP_WATCHER_AUTOMATICALLY_DISABLE_FUNCTIONS = false
                await watcher.observeResults(Array(1000).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 2,
                      "tokens": -1,
                    }
                `)
                expect(onStateChangeSpy).toHaveBeenCalledTimes(1)
                expect(onStateChangeSpy).toHaveBeenLastCalledWith({
                    hogFunction,
                    state: HogWatcherStateEnum.degraded,
                    previousState: HogWatcherStateEnum.healthy,
                })

                await watcher.observeResults(Array(1000).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect(onStateChangeSpy).toHaveBeenCalledTimes(1)
            })

            it('should not automatically transition out of disabled', async () => {
                await watcher.observeResults(Array(1000).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 3,
                      "tokens": -1,
                    }
                `)
                advanceTime(1000)
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 3,
                      "tokens": 9,
                    }
                `)

                advanceTime(1000)
                await watcher.observeResults(Array(1).fill(createResult({ duration: 10, kind: 'hog' })))
                expect(await watcher.getPersistedState(hogFunctionId)).toMatchInlineSnapshot(`
                    {
                      "state": 3,
                      "tokens": 19,
                    }
                `)
                expect(onStateChangeSpy).toHaveBeenCalledTimes(1)
            })

            it('should not change states if recently changed', async () => {
                await watcher.doStageChanges([[hogFunction, HogWatcherStateEnum.healthy]])
                await watcher.observeResults(Array(1000).fill(createResult({ duration: 1000, kind: 'hog' })))
                expect((await watcher.getPersistedState(hogFunctionId)).state).toEqual(HogWatcherStateEnum.healthy)
                const res = await redis.usePipeline({ name: 'getLock' }, (pipeline) => {
                    pipeline.get(`@posthog-test/hog-watcher-2/state-lock/${hogFunctionId}`)
                    pipeline.ttl(`@posthog-test/hog-watcher-2/state-lock/${hogFunctionId}`)
                })
                expect(res?.[0]?.[1]).toEqual('1') // The value
                expect(res?.[1]?.[1]).toBeGreaterThan(hub.CDP_WATCHER_STATE_LOCK_TTL - 5) // The ttl
                expect(res?.[1]?.[1]).toBeLessThan(hub.CDP_WATCHER_STATE_LOCK_TTL + 5) // The ttl
            })
        })
    })

    describe('doStateChanges - with resetPool', () => {
        const expectMockCaptureTeamEvent = (state: string, previousState: string) => {
            expect(mockCaptureTeamEvent).toHaveBeenCalledWith(team, 'hog_function_state_change', {
                hog_function_id: hogFunction.id,
                hog_function_type: hogFunction.type,
                hog_function_name: hogFunction.name,
                hog_function_template_id: hogFunction.template_id,
                state,
                previous_state: previousState,
            })
        }

        it('should change the state of a hog function', async () => {
            expect(await watcher.getPersistedState(hogFunction.id)).toEqual({
                state: HogWatcherStateEnum.healthy,
                tokens: 10000,
            })
            await watcher.doStageChanges([[hogFunction, HogWatcherStateEnum.degraded]], true)
            expect(await watcher.getPersistedState(hogFunction.id)).toEqual({
                state: HogWatcherStateEnum.degraded,
                tokens: 8000,
            })

            expect(onStateChangeSpy).toHaveBeenCalledWith({
                hogFunction,
                state: HogWatcherStateEnum.degraded,
                previousState: HogWatcherStateEnum.healthy,
            })
        })

        it('should only trigger state change events if the state actually changed', async () => {
            await watcher.doStageChanges([[hogFunction, HogWatcherStateEnum.degraded]], true)
            expect(onStateChangeSpy).toHaveBeenCalledTimes(1)
            expect(onStateChangeSpy).toHaveBeenLastCalledWith({
                hogFunction,
                state: HogWatcherStateEnum.degraded,
                previousState: HogWatcherStateEnum.healthy,
            })
            expectMockCaptureTeamEvent('degraded', 'healthy')

            await watcher.doStageChanges([[hogFunction, HogWatcherStateEnum.degraded]], true)
            expect(onStateChangeSpy).toHaveBeenCalledTimes(1)
            await watcher.doStageChanges([[hogFunction, HogWatcherStateEnum.disabled]], true)
            expect(onStateChangeSpy).toHaveBeenCalledTimes(2)
            expect(onStateChangeSpy).toHaveBeenLastCalledWith({
                hogFunction,
                state: HogWatcherStateEnum.disabled,
                previousState: HogWatcherStateEnum.degraded,
            })
            expectMockCaptureTeamEvent('disabled', 'degraded')
            await watcher.doStageChanges([[hogFunction, HogWatcherStateEnum.disabled]], true)
            expect(onStateChangeSpy).toHaveBeenCalledTimes(2)
        })
    })
})
