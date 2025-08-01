import {
    DataTableNode,
    DateRange,
    ErrorTrackingQuery,
    EventsQuery,
    InsightVizNode,
    NodeKind,
} from '~/queries/schema/schema-general'
import { setLatestVersionsOnQuery } from '~/queries/utils'
import {
    AnyPropertyFilter,
    BaseMathType,
    ChartDisplayType,
    ProductKey,
    PropertyGroupFilter,
    UniversalFiltersGroup,
} from '~/types'

import {
    ERROR_TRACKING_DETAILS_RESOLUTION,
    ERROR_TRACKING_LISTING_RESOLUTION,
    SEARCHABLE_EXCEPTION_PROPERTIES,
} from './utils'

export const errorTrackingQuery = ({
    orderBy,
    status,
    dateRange,
    assignee,
    filterTestAccounts,
    filterGroup,
    searchQuery,
    volumeResolution = ERROR_TRACKING_LISTING_RESOLUTION,
    columns,
    orderDirection,
    limit = 50,
}: Pick<
    ErrorTrackingQuery,
    'orderBy' | 'status' | 'dateRange' | 'assignee' | 'filterTestAccounts' | 'limit' | 'searchQuery' | 'orderDirection'
> & {
    filterGroup: UniversalFiltersGroup
    columns: ('error' | 'volume' | 'occurrences' | 'sessions' | 'users' | 'assignee' | 'library')[]
    volumeResolution?: number
}): DataTableNode => {
    return {
        kind: NodeKind.DataTableNode,
        source: {
            kind: NodeKind.ErrorTrackingQuery,
            orderBy,
            status,
            dateRange,
            assignee,
            volumeResolution,
            filterGroup: filterGroup as PropertyGroupFilter,
            filterTestAccounts: filterTestAccounts,
            searchQuery: searchQuery,
            limit: limit,
            orderDirection,
            withAggregations: true,
            withFirstEvent: false,
            tags: {
                productKey: ProductKey.ERROR_TRACKING,
            },
        },
        showActions: false,
        showTimings: false,
        columns: columns,
    }
}

export const errorTrackingIssueQuery = ({
    issueId,
    dateRange,
    filterGroup,
    filterTestAccounts,
    searchQuery,
    volumeResolution = ERROR_TRACKING_DETAILS_RESOLUTION,
    withFirstEvent = false,
    withLastEvent = false,
    withAggregations = false,
}: {
    issueId: string
    dateRange: DateRange
    filterGroup?: UniversalFiltersGroup
    filterTestAccounts: boolean
    searchQuery?: string
    volumeResolution?: number
    withFirstEvent?: boolean
    withLastEvent?: boolean
    withAggregations?: boolean
}): ErrorTrackingQuery => {
    return setLatestVersionsOnQuery<ErrorTrackingQuery>({
        kind: NodeKind.ErrorTrackingQuery,
        issueId,
        dateRange,
        filterGroup: filterGroup as PropertyGroupFilter,
        filterTestAccounts,
        searchQuery,
        volumeResolution,
        withFirstEvent,
        withAggregations,
        withLastEvent,
        tags: {
            productKey: ProductKey.ERROR_TRACKING,
        },
    })
}

export const errorTrackingIssueEventsQuery = ({
    issueId,
    filterTestAccounts,
    filterGroup,
    searchQuery,
    dateRange,
    columns,
}: {
    issueId: string | null
    filterTestAccounts: boolean
    filterGroup: UniversalFiltersGroup
    searchQuery: string
    dateRange: DateRange
    columns: string[]
}): EventsQuery => {
    if (!issueId) {
        throw new Error('issue id is required')
    }

    const group = filterGroup.values[0] as UniversalFiltersGroup
    const properties = [...group.values] as AnyPropertyFilter[]

    let where_string = `'${issueId}' == issue_id`
    if (searchQuery) {
        // This is an ugly hack for the fact I don't think we support nested property filters in
        // the eventsquery
        where_string += ' AND ('
        const chunks: string[] = []
        SEARCHABLE_EXCEPTION_PROPERTIES.forEach((prop) => {
            chunks.push(`ilike(toString(properties.${prop}), '%${searchQuery}%')`)
        })
        where_string += chunks.join(' OR ')
        where_string += ')'
    }

    const where = [where_string]

    const eventsQuery: EventsQuery = {
        kind: NodeKind.EventsQuery,
        event: '$exception',
        select: columns,
        where,
        properties,
        filterTestAccounts: filterTestAccounts,
        after: dateRange.date_from ?? undefined,
        before: dateRange.date_to ?? undefined,
    }

    return eventsQuery
}

export const errorTrackingIssueBreakdownQuery = ({
    breakdownProperty,
    dateRange,
    filterTestAccounts,
    filterGroup,
}: {
    breakdownProperty: string
    dateRange: DateRange
    filterTestAccounts: boolean
    filterGroup: UniversalFiltersGroup
}): InsightVizNode => {
    return {
        kind: NodeKind.InsightVizNode,
        source: {
            kind: NodeKind.TrendsQuery,
            trendsFilter: {
                display: ChartDisplayType.ActionsBarValue,
            },
            breakdownFilter: {
                breakdown_type: 'event',
                breakdown: breakdownProperty,
                breakdown_limit: 10,
            },
            series: [
                {
                    kind: NodeKind.EventsNode,
                    event: '$exception',
                    math: BaseMathType.TotalCount,
                },
            ],
            dateRange: dateRange,
            properties: filterGroup.values as AnyPropertyFilter[],
            filterTestAccounts,
        },
    }
}
